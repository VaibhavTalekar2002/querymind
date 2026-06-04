import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import {
  BarChart, Bar, LineChart, Line, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer
} from "recharts";
import "./App.css";

const API = import.meta.env.VITE_API_URL || "https://querymind-pwqq.onrender.com";
const CHART_COLORS = ["#00f5d4", "#f72585", "#fee440", "#7209b7", "#4cc9f0", "#f3722c"];
const PAGE_SIZE = 20;
const BOOKMARKS_KEY = "querymind_bookmarks";
const REQUEST_TIMEOUT_MS = 30000;
const UPLOAD_TIMEOUT_MS = 120000; // 2 min for file uploads
const DANGEROUS_SQL_KEYWORDS = ["delete", "drop", "truncate", "update", "insert", "alter", "replace"];

function hasDangerousKeyword(text = "") {
  return DANGEROUS_SQL_KEYWORDS.find(keyword => new RegExp(`\\b${keyword}\\b`, "i").test(text));
}

async function safeJson(res) {
  try { return await res.json(); }
  catch { return {}; }
}

function getApiError(data, fallback = "Something went wrong. Please try again.") {
  return data?.detail || data?.error || data?.message || fallback;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timeoutId);
  }
}

function normalizeQueryResponse(data = {}, question = "") {
  const results = Array.isArray(data.results) ? data.results : [];
  const totalRows = Number.isFinite(Number(data.total_rows)) ? Number(data.total_rows) : results.length;
  return {
    ...data,
    question: data.question || question,
    results,
    total_rows: totalRows,
    generated_sql: data.generated_sql || "",
    explanation: data.explanation || "",
    active_db: data.active_db || "",
  };
}

function detectChartType(rows) {
  const safeRows = Array.isArray(rows) ? rows.filter(row => row && typeof row === "object") : [];
  if (safeRows.length < 2) return null;

  const keys = Object.keys(safeRows[0] || {});
  if (keys.length < 2) return null;

  const populatedRows = safeRows.filter(row => keys.some(key => row[key] !== null && row[key] !== undefined && row[key] !== ""));
  if (populatedRows.length < 2) return null;

  const isNumericColumn = (key) => {
    const values = populatedRows.map(row => row[key]).filter(v => v !== null && v !== undefined && v !== "");
    return values.length >= 2 && values.every(v => Number.isFinite(Number(v)));
  };

  const isLabelColumn = (key) => {
    if (isNumericColumn(key)) return false;
    const values = populatedRows.map(row => row[key]).filter(v => v !== null && v !== undefined && String(v).trim() !== "");
    return values.length >= 2 && values.some(v => Number.isNaN(Number(v)));
  };

  const numericKeys = keys.filter(isNumericColumn);
  const labelKeys = keys.filter(isLabelColumn);
  const labelKey = labelKeys.find(k => /name|label|category|type|status|date|month|year|state|city|country/i.test(k)) || labelKeys[0];
  const valueKey = numericKeys.find(k => /count|total|sum|avg|average|amount|value|revenue|sales|rows/i.test(k)) || numericKeys[0];

  if (labelKey && valueKey) {
    const validPoints = populatedRows.filter(row => String(row[labelKey] ?? "").trim() && Number.isFinite(Number(row[valueKey])));
    if (validPoints.length < 2) return null;
    return { type: validPoints.length <= 6 ? "pie" : "bar", labelKey, valueKey };
  }

  if (numericKeys.length >= 2) {
    const valueKey = numericKeys[1];
    const validPoints = populatedRows.filter(row => Number.isFinite(Number(row[valueKey])));
    if (validPoints.length < 2) return null;
    return { type: "line", labelKey: numericKeys[0], valueKey };
  }

  return null;
}

function exportToCSV(rows, filename = "results.csv") {
  const safeRows = Array.isArray(rows) ? rows : [];
  if (!safeRows.length) return;
  const headers = Object.keys(safeRows[0] || {});
  const escapeCsv = (value) => {
    if (value === null || value === undefined) return "";
    const str = String(value).replace(/"/g, '""');
    return /[",\r\n]/.test(str) ? `"${str}"` : str;
  };
  const csvRows = safeRows.map(r => headers.map(h => escapeCsv(r?.[h])).join(","));
  const headerRow = headers.map(escapeCsv).join(",");
  const blob = new Blob([[headerRow, ...csvRows].join("\n")], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function loadBookmarks() {
  try { return JSON.parse(localStorage.getItem(BOOKMARKS_KEY) || "[]"); }
  catch { return []; }
}
function saveBookmarks(bm) { localStorage.setItem(BOOKMARKS_KEY, JSON.stringify(bm)); }

// ── Typewriter ────────────────────────────────────────────────────────────────
function useTypewriter(text, speed = 14) {
  const [displayed, setDisplayed] = useState("");
  const [done, setDone] = useState(false);
  const intervalRef = useRef(null);
  useEffect(() => {
    const safeText = String(text || "");
    if (intervalRef.current) clearInterval(intervalRef.current);
    let resetId;

    if (!safeText) {
      resetId = setTimeout(() => {
        setDisplayed("");
        setDone(true);
      }, 0);
      return () => {
        clearTimeout(resetId);
        if (intervalRef.current) {
          clearInterval(intervalRef.current);
          intervalRef.current = null;
        }
      };
    }

    resetId = setTimeout(() => {
      setDisplayed("");
      setDone(false);
      let i = 0;
      intervalRef.current = setInterval(() => {
        i++;
        setDisplayed(safeText.slice(0, i));
        if (i >= safeText.length) {
          clearInterval(intervalRef.current);
          intervalRef.current = null;
          setDone(true);
        }
      }, speed);
    }, 0);

    return () => {
      clearTimeout(resetId);
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [text, speed]);
  return { displayed, done };
}

// ── SQL Block ─────────────────────────────────────────────────────────────────
function SQLBlock({ sql, autoFixed, activeDb, onBookmark, isBookmarked }) {
  const { displayed, done } = useTypewriter(sql, 14);
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(sql);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="sql-block">
      <div className="sql-block-header">
        <span className="section-label">GENERATED SQL</span>
        {autoFixed && <span className="badge-fix">⚡ AUTO-FIXED</span>}
        {activeDb && <span className="db-tag">📦 {activeDb}</span>}
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button className={`bookmark-btn ${isBookmarked ? "bookmarked" : ""}`} onClick={onBookmark}>
            {isBookmarked ? "★" : "☆"}
          </button>
          <button className="copy-btn" onClick={handleCopy}>{copied ? "✓ Copied" : "Copy"}</button>
        </div>
      </div>
      <pre className="sql-code">
        {displayed}
        {!done && <span className="cursor-blink">▋</span>}
      </pre>
    </div>
  );
}

const ERD_CARD_WIDTH = 230;
const ERD_HEADER_HEIGHT = 36;
const ERD_COLUMN_HEIGHT = 27;
const ERD_CARD_GAP_X = 88;
const ERD_CARD_GAP_Y = 78;

function getSchemaRelationships(schema = []) {
  const tableNames = new Set(schema.map(t => t.table));
  return schema.flatMap(table =>
    (table.columns || [])
      .filter(col => col.is_fk && col.fk_ref_table && tableNames.has(col.fk_ref_table))
      .map(col => ({
        id: `${table.table}.${col.name}->${col.fk_ref_table}.${col.fk_ref_col || "id"}`,
        fromTable: table.table,
        fromColumn: col.name,
        toTable: col.fk_ref_table,
        toColumn: col.fk_ref_col,
      }))
  );
}

function getInitialErdPositions(schema = []) {
  const columns = Math.max(1, Math.ceil(Math.sqrt(schema.length || 1)));
  return schema.reduce((acc, table, index) => {
    const col = index % columns;
    const row = Math.floor(index / columns);
    acc[table.table] = {
      x: 34 + col * (ERD_CARD_WIDTH + ERD_CARD_GAP_X),
      y: 34 + row * (210 + ERD_CARD_GAP_Y),
    };
    return acc;
  }, {});
}

function getErdCardHeight(table) {
  const visibleColumns = Math.min((table.columns || []).length, 12);
  return ERD_HEADER_HEIGHT + visibleColumns * ERD_COLUMN_HEIGHT + 10;
}

function getRelationshipPath(rel, schemaByName, positions) {
  const fromTable = schemaByName.get(rel.fromTable);
  const toTable = schemaByName.get(rel.toTable);
  const fromPos = positions[rel.fromTable];
  const toPos = positions[rel.toTable];
  if (!fromTable || !toTable || !fromPos || !toPos) return null;

  const fromIndex = Math.max(0, (fromTable.columns || []).findIndex(c => c.name === rel.fromColumn));
  const toIndexRaw = (toTable.columns || []).findIndex(c => c.name === rel.toColumn);
  const toIndex = toIndexRaw >= 0 ? toIndexRaw : Math.max(0, (toTable.columns || []).findIndex(c => c.is_pk));
  const fromRight = fromPos.x < toPos.x;
  const startX = fromRight ? fromPos.x + ERD_CARD_WIDTH : fromPos.x;
  const endX = fromRight ? toPos.x : toPos.x + ERD_CARD_WIDTH;
  const startY = fromPos.y + ERD_HEADER_HEIGHT + fromIndex * ERD_COLUMN_HEIGHT + ERD_COLUMN_HEIGHT / 2;
  const endY = toPos.y + ERD_HEADER_HEIGHT + Math.max(0, toIndex) * ERD_COLUMN_HEIGHT + ERD_COLUMN_HEIGHT / 2;
  const curve = Math.max(70, Math.abs(endX - startX) * 0.42);
  const c1x = startX + (fromRight ? curve : -curve);
  const c2x = endX - (fromRight ? curve : -curve);
  return `M ${startX} ${startY} C ${c1x} ${startY}, ${c2x} ${endY}, ${endX} ${endY}`;
}

function ErdViewer({ schema }) {
  const dragRef = useRef(null);
  const panRef = useRef(null);
  const [positions, setPositions] = useState(() => getInitialErdPositions(schema));
  const [viewport, setViewport] = useState({ x: 0, y: 0, zoom: 1 });
  const [hoveredRel, setHoveredRel] = useState(null);

  const schemaKey = useMemo(() => schema.map(t => `${t.table}:${t.columns?.length || 0}`).join("|"), [schema]);
  const schemaByName = useMemo(() => new Map(schema.map(t => [t.table, t])), [schema]);
  const relationships = useMemo(() => getSchemaRelationships(schema), [schema]);
  const canvasSize = useMemo(() => {
    const values = schema.map(table => {
      const pos = positions[table.table] || { x: 0, y: 0 };
      return {
        right: pos.x + ERD_CARD_WIDTH + 80,
        bottom: pos.y + getErdCardHeight(table) + 80,
      };
    });
    return {
      width: Math.max(980, ...values.map(v => v.right), 0),
      height: Math.max(620, ...values.map(v => v.bottom), 0),
    };
  }, [schema, positions]);

  useEffect(() => {
    const resetId = setTimeout(() => {
      setPositions(getInitialErdPositions(schema));
      setViewport({ x: 0, y: 0, zoom: 1 });
      setHoveredRel(null);
    }, 0);
    return () => clearTimeout(resetId);
  }, [schemaKey, schema]);

  const screenToCanvasDelta = useCallback((delta) => delta / viewport.zoom, [viewport.zoom]);

  const handleCardPointerDown = (event, tableName) => {
    event.preventDefault();
    event.stopPropagation();
    const pos = positions[tableName] || { x: 0, y: 0 };
    dragRef.current = {
      tableName,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: pos.x,
      originY: pos.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handleCardPointerMove = (event) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = screenToCanvasDelta(event.clientX - drag.startX);
    const dy = screenToCanvasDelta(event.clientY - drag.startY);
    setPositions(prev => ({
      ...prev,
      [drag.tableName]: {
        x: Math.max(0, drag.originX + dx),
        y: Math.max(0, drag.originY + dy),
      },
    }));
  };

  const stopCardDrag = (event) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
  };

  const handlePanPointerDown = (event) => {
    if (event.target.closest(".erd-card") || event.target.closest(".erd-control-btn")) return;
    panRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: viewport.x,
      originY: viewport.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handlePanPointerMove = (event) => {
    const pan = panRef.current;
    if (!pan || pan.pointerId !== event.pointerId) return;
    setViewport(prev => ({
      ...prev,
      x: pan.originX + event.clientX - pan.startX,
      y: pan.originY + event.clientY - pan.startY,
    }));
  };

  const stopPan = (event) => {
    if (panRef.current?.pointerId === event.pointerId) panRef.current = null;
  };

  const zoomBy = (amount) => {
    setViewport(prev => ({
      ...prev,
      zoom: Math.min(1.6, Math.max(0.55, Number((prev.zoom + amount).toFixed(2)))),
    }));
  };

  const resetView = () => {
    setViewport({ x: 0, y: 0, zoom: 1 });
    setPositions(getInitialErdPositions(schema));
  };

  const highlightedTables = useMemo(() => {
    const rel = relationships.find(r => r.id === hoveredRel);
    return rel ? new Set([rel.fromTable, rel.toTable]) : new Set();
  }, [hoveredRel, relationships]);

  return (
    <div className="erd-shell">
      <div className="erd-toolbar">
        <span className="erd-hint">Drag tables. Drag empty canvas to pan. FK relationships are dashed.</span>
        <div className="erd-controls">
          <button className="erd-control-btn" onClick={() => zoomBy(-0.1)}>−</button>
          <span className="erd-zoom-label">{Math.round(viewport.zoom * 100)}%</span>
          <button className="erd-control-btn" onClick={() => zoomBy(0.1)}>+</button>
          <button className="erd-control-btn" onClick={resetView}>Reset</button>
        </div>
      </div>

      <div
        className="erd-canvas"
        onPointerDown={handlePanPointerDown}
        onPointerMove={handlePanPointerMove}
        onPointerUp={stopPan}
        onPointerCancel={stopPan}
      >
        <div
          className="erd-stage"
          style={{
            width: canvasSize.width,
            height: canvasSize.height,
            transform: `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`,
          }}
        >
          <svg className="erd-svg-layer" width={canvasSize.width} height={canvasSize.height}>
            <defs>
              <filter id="erdLineGlow" x="-30%" y="-30%" width="160%" height="160%">
                <feGaussianBlur stdDeviation="2.4" result="blur" />
                <feMerge>
                  <feMergeNode in="blur" />
                  <feMergeNode in="SourceGraphic" />
                </feMerge>
              </filter>
            </defs>
            {relationships.map(rel => {
              const path = getRelationshipPath(rel, schemaByName, positions);
              if (!path) return null;
              const active = hoveredRel === rel.id;
              return (
                <path
                  key={rel.id}
                  className={`erd-relationship-line ${active ? "active" : ""}`}
                  d={path}
                  onPointerEnter={() => setHoveredRel(rel.id)}
                  onPointerLeave={() => setHoveredRel(null)}
                />
              );
            })}
          </svg>

          {schema.map(table => {
            const pos = positions[table.table] || { x: 0, y: 0 };
            const related = highlightedTables.has(table.table);
            const visibleColumns = (table.columns || []).slice(0, 12);
            const hiddenCount = Math.max(0, (table.columns || []).length - visibleColumns.length);
            return (
              <div
                key={table.table}
                className={`erd-card ${related ? "related" : ""}`}
                style={{ transform: `translate(${pos.x}px, ${pos.y}px)` }}
                onPointerDown={(event) => handleCardPointerDown(event, table.table)}
                onPointerMove={handleCardPointerMove}
                onPointerUp={stopCardDrag}
                onPointerCancel={stopCardDrag}
              >
                <div className="erd-header">
                  <span className="erd-table-name">{table.table}</span>
                  <span className="erd-row-count">{Number(table.row_count || 0).toLocaleString()} rows</span>
                </div>
                <div className="erd-columns">
                  {visibleColumns.map(col => (
                    <div
                      key={col.name}
                      className={`erd-column ${col.is_pk ? "pk" : ""} ${col.is_fk ? "fk" : ""}`}
                      title={col.is_fk ? `${col.name} → ${col.fk_ref_table}.${col.fk_ref_col}` : col.name}
                    >
                      <span className="erd-column-badges">
                        {col.is_pk && <span className="badge-pk">PK</span>}
                        {col.is_fk && <span className="badge-fk">FK</span>}
                      </span>
                      <span className="erd-column-name">{col.name}</span>
                      <span className="erd-column-type">{col.type}</span>
                    </div>
                  ))}
                  {hiddenCount > 0 && <div className="erd-more-columns">+ {hiddenCount} more columns</div>}
                </div>
              </div>
            );
          })}
        </div>

        <div className="erd-minimap">
          <div
            className="erd-minimap-window"
            style={{
              width: `${Math.max(20, 100 / viewport.zoom)}%`,
              height: `${Math.max(20, 100 / viewport.zoom)}%`,
              transform: `translate(${Math.min(70, Math.max(0, -viewport.x / 20))}%, ${Math.min(70, Math.max(0, -viewport.y / 20))}%)`,
            }}
          />
        </div>
      </div>
    </div>
  );
}

// ── Enhanced Schema Panel — ALL tables expanded by default ────────────────────
function SchemaPanel({ schema, activeDb, onClose }) {
  // ALL tables expanded by default
  const [expandedTables, setExpandedTables] = useState(() => {
    const s = new Set();
    schema.forEach(t => s.add(t.table));
    return s;
  });
  const [search, setSearch] = useState("");
  const [schemaView, setSchemaView] = useState("list");

  // When schema loads, expand all
  useEffect(() => {
    const resetId = setTimeout(() => {
      const s = new Set();
      schema.forEach(t => s.add(t.table));
      setExpandedTables(s);
    }, 0);
    return () => clearTimeout(resetId);
  }, [schema]);

  const toggleTable = (name) => {
    setExpandedTables(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const filtered = schema.filter(t =>
    t.table.toLowerCase().includes(search.toLowerCase()) ||
    t.columns.some(c => c.name.toLowerCase().includes(search.toLowerCase()))
  );

  const totalTables = schema.length;
  const totalRows = schema.reduce((sum, t) => sum + t.row_count, 0);
  const totalCols = schema.reduce((sum, t) => sum + t.columns.length, 0);

  return (
    <div className="schema-overlay" onClick={onClose}>
      <div className={`schema-panel wide-panel ${schemaView === "erd" ? "erd-panel" : ""}`} onClick={e => e.stopPropagation()}>
        <div className="schema-header">
          <div>
            <span className="schema-title">DATABASE SCHEMA</span>
            <div style={{ fontSize: 11, color: "var(--accent)", marginTop: 2, fontFamily: "var(--mono)" }}>{activeDb}</div>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <div className="schema-view-tabs">
              <button className={`schema-view-tab ${schemaView === "list" ? "active" : ""}`} onClick={() => setSchemaView("list")}>List View</button>
              <button className={`schema-view-tab ${schemaView === "erd" ? "active" : ""}`} onClick={() => setSchemaView("erd")}>ERD View</button>
            </div>
            {schemaView === "list" && <button className="expand-all-btn" onClick={() => {
              if (expandedTables.size === schema.length) setExpandedTables(new Set());
              else { const s = new Set(); schema.forEach(t => s.add(t.table)); setExpandedTables(s); }
            }}>
              {expandedTables.size === schema.length ? "Collapse All" : "Expand All"}
            </button>}
            <button className="icon-btn" onClick={onClose}>✕</button>
          </div>
        </div>

        {/* DB Stats */}
        <div className="schema-stats">
          <div className="schema-stat">
            <div className="schema-stat-val">{totalTables}</div>
            <div className="schema-stat-label">Tables</div>
          </div>
          <div className="schema-stat">
            <div className="schema-stat-val">{totalCols}</div>
            <div className="schema-stat-label">Columns</div>
          </div>
          <div className="schema-stat">
            <div className="schema-stat-val">{totalRows.toLocaleString()}</div>
            <div className="schema-stat-label">Total Rows</div>
          </div>
        </div>

        {schemaView === "list" ? (
          <>
        {/* Search */}
        <div className="schema-search-wrap">
          <span className="filter-icon">⌕</span>
          <input
            className="schema-search"
            placeholder="Search tables or columns..."
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
          {search && <button className="filter-clear" onClick={() => setSearch("")}>✕</button>}
        </div>

        <div className="schema-body">
          {filtered.map(t => {
            const isExpanded = expandedTables.has(t.table) || search.length > 0;
            return (
              <div key={t.table} className="schema-table">
                <div className="schema-table-name" onClick={() => toggleTable(t.table)}>
                  <span className="dot green" />
                  <span className="schema-tbl-icon">▤</span>
                  <span className="schema-tbl-name">{t.table}</span>
                  <span className="row-badge">{t.row_count.toLocaleString()} rows</span>
                  <span className="col-count-badge">{t.columns.length} cols</span>
                  <span className="expand-arrow">{isExpanded ? "▴" : "▾"}</span>
                </div>

                {isExpanded && (
                  <div className="schema-columns">
                    {/* Legend */}
                    <div className="schema-legend">
                      <span className="legend-item"><span className="badge-pk">PK</span> Primary Key</span>
                      <span className="legend-item"><span className="badge-fk">FK</span> Foreign Key</span>
                      <span className="legend-item"><span className="badge-nn">NN</span> Not Null</span>
                      <span className="legend-item"><span className="badge-uq">UQ</span> Unique</span>
                    </div>
                    {t.columns.map(c => (
                      <div key={c.name} className={`schema-col-row ${c.is_pk ? "col-pk" : ""} ${c.is_fk ? "col-fk" : ""}`}>
                        <div className="col-badges">
                          {c.is_pk && <span className="badge-pk">PK</span>}
                          {c.is_fk && <span className="badge-fk">FK</span>}
                          {c.not_null && !c.is_pk && <span className="badge-nn">NN</span>}
                          {c.unique && !c.is_pk && <span className="badge-uq">UQ</span>}
                        </div>
                        <span className="col-name-rich">{c.name}</span>
                        <span className="col-type-rich">{c.type}</span>
                        {c.is_fk && (
                          <span className="col-fk-ref">→ {c.fk_ref_table}.{c.fk_ref_col}</span>
                        )}
                        {c.default !== null && c.default !== undefined && (
                          <span className="col-default">= {c.default}</span>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
          </>
        ) : (
          <ErdViewer schema={schema} />
        )}
      </div>
    </div>
  );
}

// ── Bookmarks Panel ───────────────────────────────────────────────────────────
function BookmarksPanel({ bookmarks, onSelect, onDelete, onClose }) {
  return (
    <div className="schema-overlay" onClick={onClose}>
      <div className="schema-panel" onClick={e => e.stopPropagation()}>
        <div className="schema-header">
          <span className="schema-title">BOOKMARKS</span>
          <button className="icon-btn" onClick={onClose}>✕</button>
        </div>
        <div className="schema-body">
          {bookmarks.length === 0 && (
            <div className="empty-state">
              <div className="empty-icon">☆</div>
              <p>No bookmarks yet.</p>
              <p className="empty-sub">Click ☆ on any query result to save it here.</p>
            </div>
          )}
          {bookmarks.map(b => (
            <div key={b.id} className="bookmark-item">
              <div className="bookmark-question" onClick={() => { onSelect(b.question); onClose(); }}>
                <span className="bookmark-star">★</span>{b.question}
              </div>
              <div className="bookmark-meta">
                <span className="db-tag">📦 {b.database}</span>
                <span>{new Date(b.savedAt).toLocaleDateString()}</span>
                <button className="bm-delete-btn" onClick={() => onDelete(b.id)}>✕</button>
              </div>
              <div className="bookmark-sql-preview">{b.sql}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Data Table — NO height limit, full display ────────────────────────────────
function DataTable({ rows }) {
  const safeRows = useMemo(() => (
    Array.isArray(rows) ? rows.filter(row => row && typeof row === "object") : []
  ), [rows]);
  const [filter, setFilter] = useState("");
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState("asc");
  const [page, setPage] = useState(1);

  useEffect(() => {
    const resetId = setTimeout(() => {
      setPage(1);
      setFilter("");
      setSortCol(null);
    }, 0);
    return () => clearTimeout(resetId);
  }, [rows]);

  const columns = safeRows.length > 0 ? Object.keys(safeRows[0]) : [];

  const filtered = useMemo(() => {
    if (!filter.trim()) return safeRows;
    const q = filter.toLowerCase();
    return safeRows.filter(row => Object.values(row).some(v => String(v ?? "").toLowerCase().includes(q)));
  }, [safeRows, filter]);

  const sorted = useMemo(() => {
    if (!sortCol) return filtered;
    return [...filtered].sort((a, b) => {
      const av = a[sortCol] ?? ""; const bv = b[sortCol] ?? "";
      const an = parseFloat(av); const bn = parseFloat(bv);
      const isNum = !isNaN(an) && !isNaN(bn);
      const cmp = isNum ? an - bn : String(av).localeCompare(String(bv));
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [filtered, sortCol, sortDir]);

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const paginated = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const handleSort = (col) => {
    if (sortCol === col) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortCol(col); setSortDir("asc"); }
    setPage(1);
  };

  if (safeRows.length === 0) return <p className="no-results">No results returned.</p>;

  return (
    <div className="datatable-wrapper">
      <div className="filter-bar">
        <div className="filter-input-wrap">
          <span className="filter-icon">⌕</span>
          <input className="filter-input" placeholder="Filter results..."
            value={filter} onChange={e => { setFilter(e.target.value); setPage(1); }} />
          {filter && <button className="filter-clear" onClick={() => { setFilter(""); setPage(1); }}>✕</button>}
        </div>
        <div className="filter-meta">
          {filter
            ? <span><strong style={{ color: "var(--accent)" }}>{filtered.length}</strong> of {safeRows.length} rows</span>
            : <span><strong style={{ color: "var(--text)" }}>{safeRows.length}</strong> rows total</span>}
        </div>
      </div>

      {/* Full-width scrollable table — NO max-height */}
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th className="sortable-th row-num-th"><div className="th-inner">#</div></th>
              {columns.map(col => (
                <th key={col} onClick={() => handleSort(col)} className="sortable-th">
                  <div className="th-inner">
                    {col}
                    <span className="sort-icon">{sortCol === col ? (sortDir === "asc" ? "↑" : "↓") : "↕"}</span>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paginated.map((row, i) => (
              <tr key={i}>
                <td className="row-num-cell">{(page - 1) * PAGE_SIZE + i + 1}</td>
                {columns.map((col, j) => (
                  <td key={j} title={String(row?.[col] ?? "")}>{row?.[col] ?? "—"}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div className="pagination">
          <button className="page-btn" onClick={() => setPage(1)} disabled={page === 1}>«</button>
          <button className="page-btn" onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1}>‹ Prev</button>
          <div className="page-numbers">
            {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
              let p;
              if (totalPages <= 5) p = i + 1;
              else if (page <= 3) p = i + 1;
              else if (page >= totalPages - 2) p = totalPages - 4 + i;
              else p = page - 2 + i;
              return <button key={p} className={`page-num ${page === p ? "active" : ""}`} onClick={() => setPage(p)}>{p}</button>;
            })}
          </div>
          <button className="page-btn" onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page === totalPages}>Next ›</button>
          <button className="page-btn" onClick={() => setPage(totalPages)} disabled={page === totalPages}>»</button>
          <span className="page-info">Page <strong>{page}</strong> of <strong>{totalPages}</strong></span>
        </div>
      )}
    </div>
  );
}

// ── DB Switcher ───────────────────────────────────────────────────────────────
const DB_TYPES = [
  { value: "sqlite",     label: "SQLite",     icon: "🗃", port: 0 },
  { value: "postgresql", label: "PostgreSQL", icon: "🐘", port: 5432 },
  { value: "mysql",      label: "MySQL",      icon: "🐬", port: 3306 },
  { value: "mssql",      label: "SQL Server", icon: "🪟", port: 1433 },
];

function DBSwitcherPanel({ connections, activeConnId, onSwitch, onUploadDb, onConnected, onRefresh, onClose }) {
  const dbFileRef = useRef();
  const [uploading, setUploading] = useState(false);
  const [tab, setTab] = useState("list"); // list | add
  const [form, setForm] = useState({ name: "", type: "postgresql", host: "", port: 5432, database: "", username: "", password: "" });
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [saving, setSaving] = useState(false);
  const [uploadError, setUploadError] = useState("");

  useEffect(() => {
    // The connection registry can change after CSV uploads, so refresh when
    // the switcher opens instead of trusting whatever state the header had.
    onRefresh?.();
  }, [onRefresh]);

  const handleDbUpload = async (e) => {
    const file = e.target.files[0]; if (!file) return;
    setUploading(true); setUploadError("");
    const formData = new FormData(); formData.append("file", file);
    try {
      const res = await fetchWithTimeout(`${API}/connections/upload-db`, { method: "POST", body: formData });
      const data = await safeJson(res);
      if (!res.ok) throw new Error(getApiError(data, "Database upload failed."));
      if (data.detail || data.error) throw new Error(getApiError(data, "Database upload failed."));
      if (data.success) await onUploadDb(data);
      else setUploadError(getApiError(data, "Database upload failed."));
    } catch (err) {
      setUploadError(err.name === "AbortError" ? "Database upload timed out. Please try again." : err.message);
    }
    finally { setUploading(false); e.target.value = ""; }
  };

  const handleTypeChange = (type) => {
    const t = DB_TYPES.find(d => d.value === type);
    setForm(f => ({ ...f, type, port: t?.port || 0 }));
    setTestResult(null);
  };

  const handleTest = async () => {
    setTesting(true); setTestResult(null);
    try {
      const res = await fetchWithTimeout(`${API}/connections/test`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form)
      });
      const data = await safeJson(res);
      if (!res.ok || data.detail || data.error) {
        setTestResult({ success: false, message: getApiError(data, "Connection test failed.") });
      } else {
        setTestResult({ ...data, message: data.message || "Connection test completed." });
      }
    } catch (e) { setTestResult({ success: false, message: e.name === "AbortError" ? "Connection test timed out. Please try again." : e.message }); }
    finally { setTesting(false); }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const res = await fetchWithTimeout(`${API}/connections/add`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form)
      });
      const data = await safeJson(res);
      if (!res.ok) throw new Error(getApiError(data, "Could not save connection."));
      if (data.detail || data.error) throw new Error(getApiError(data, "Could not save connection."));
      if (data.success) { onConnected(data); setTab("list"); setForm({ name: "", type: "postgresql", host: "", port: 5432, database: "", username: "", password: "" }); setTestResult(null); }
      else setTestResult({ success: false, message: getApiError(data, "Could not save connection.") });
    } catch (e) { setTestResult({ success: false, message: e.name === "AbortError" ? "Saving the connection timed out. Please try again." : e.message }); }
    finally { setSaving(false); }
  };

  const handleDelete = async (connId, e) => {
    e.stopPropagation();
    if (!confirm("Remove this connection?")) return;
    try {
      const deleteRes = await fetchWithTimeout(`${API}/connections/${connId}`, { method: "DELETE" });
      const deleteData = await safeJson(deleteRes);
      if (!deleteRes.ok) throw new Error(getApiError(deleteData, "Could not remove connection."));
      const listRes = await fetchWithTimeout(`${API}/connections`);
      const listData = await safeJson(listRes);
      if (!listRes.ok) throw new Error(getApiError(listData, "Could not refresh connections."));
      onConnected({ connections: Array.isArray(listData.connections) ? listData.connections : [] });
    } catch (err) {
      setUploadError(err.name === "AbortError" ? "Connection request timed out. Please try again." : err.message);
    }
  };

  return (
    <div className="schema-overlay" onClick={onClose}>
      <div className="schema-panel conn-panel" onClick={e => e.stopPropagation()}>
        <div className="schema-header">
          <span className="schema-title">DATABASE CONNECTIONS</span>
          <div style={{ display: "flex", gap: 8 }}>
            <div className="schema-view-tabs">
              <button className={`schema-view-tab ${tab === "list" ? "active" : ""}`} onClick={() => setTab("list")}>≡ Connections</button>
              <button className={`schema-view-tab ${tab === "add" ? "active" : ""}`} onClick={() => setTab("add")}>+ New</button>
            </div>
            <button className="icon-btn" onClick={onClose}>✕</button>
          </div>
        </div>

        {tab === "list" ? (
          <div className="schema-body">
            {/* Upload SQLite */}
            <div className="db-upload-box" onClick={() => dbFileRef.current.click()}>
              <span className="db-upload-icon">⬆</span>
              <div>
                <div className="db-upload-title">{uploading ? "Uploading..." : "Upload SQLite .db file"}</div>
                <div className="db-upload-sub">Instantly connect any SQLite database</div>
              </div>
              <input ref={dbFileRef} type="file" accept=".db" style={{ display: "none" }} onChange={handleDbUpload} />
            </div>
            {uploadError && <div className="conn-test-result error">{uploadError}</div>}

            <div className="section-label">SAVED CONNECTIONS</div>
            {connections.map(c => (
              <div key={c.id} className={`db-item ${c.id === activeConnId ? "active" : ""}`} onClick={() => onSwitch(c.id)}>
                <span className="conn-type-icon">{DB_TYPES.find(d => d.value === c.type)?.icon || "🗄"}</span>
                <div className="conn-info">
                  <div className="conn-name">{c.name}</div>
                  <div className="conn-type-label">{c.type?.toUpperCase()}</div>
                </div>
                {c.id === activeConnId && <span className="db-active-badge">ACTIVE</span>}
                {c.id !== "default" && (
                  <button className="conn-delete-btn" onClick={(e) => handleDelete(c.id, e)}>✕</button>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="schema-body">
            <div className="conn-form">
              {/* DB Type selector */}
              <div className="form-group">
                <label className="form-label">DATABASE TYPE</label>
                <div className="conn-type-grid">
                  {DB_TYPES.map(t => (
                    <button key={t.value} className={`db-type-btn ${form.type === t.value ? "active" : ""}`}
                      onClick={() => handleTypeChange(t.value)}>
                      <span className="db-type-icon">{t.icon}</span>
                      <span className="db-type-label">{t.label}</span>
                    </button>
                  ))}
                </div>
              </div>

              <div className="form-group">
                <label className="form-label">CONNECTION NAME</label>
                <input className="form-input" placeholder="e.g. Production DB"
                  value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />
              </div>

              {form.type !== "sqlite" ? (
                <>
                  <div className="form-row">
                    <div className="form-group" style={{ flex: 2 }}>
                      <label className="form-label">HOST</label>
                      <input className="form-input" placeholder="localhost or IP"
                        value={form.host} onChange={e => setForm(f => ({ ...f, host: e.target.value }))} />
                    </div>
                    <div className="form-group" style={{ flex: 1 }}>
                      <label className="form-label">PORT</label>
                      <input className="form-input" type="number" placeholder={form.port}
                        value={form.port} onChange={e => setForm(f => ({ ...f, port: parseInt(e.target.value) || 0 }))} />
                    </div>
                  </div>
                  <div className="form-group">
                    <label className="form-label">DATABASE NAME</label>
                    <input className="form-input" placeholder="database name"
                      value={form.database} onChange={e => setForm(f => ({ ...f, database: e.target.value }))} />
                  </div>
                  <div className="form-row">
                    <div className="form-group" style={{ flex: 1 }}>
                      <label className="form-label">USERNAME</label>
                      <input className="form-input" placeholder="username"
                        value={form.username} onChange={e => setForm(f => ({ ...f, username: e.target.value }))} />
                    </div>
                    <div className="form-group" style={{ flex: 1 }}>
                      <label className="form-label">PASSWORD</label>
                      <input className="form-input" type="password" placeholder="••••••••"
                        value={form.password} onChange={e => setForm(f => ({ ...f, password: e.target.value }))} />
                    </div>
                  </div>
                </>
              ) : (
                <div className="form-group">
                  <label className="form-label">DATABASE PATH</label>
                  <input className="form-input" placeholder="/path/to/database.db"
                    value={form.database} onChange={e => setForm(f => ({ ...f, database: e.target.value }))} />
                </div>
              )}

              {testResult && (
                <div className={`conn-test-result ${testResult.success ? "success" : "error"}`}>
                  {testResult.success ? "✓" : "✗"} {testResult.message}
                </div>
              )}

              <div className="form-actions">
                <button className="test-conn-btn" onClick={handleTest} disabled={testing || !form.name}>
                  {testing ? <><span className="spinner-dark" /> Testing...</> : "⚡ Test Connection"}
                </button>
                <button className="save-conn-btn" onClick={handleSave}
                  disabled={saving || !testResult?.success}>
                  {saving ? "Saving..." : "✓ Save & Connect"}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── History Panel ─────────────────────────────────────────────────────────────
function HistoryPanel({ history, onSelect, onClear, onClose }) {
  return (
    <div className="schema-overlay" onClick={onClose}>
      <div className="schema-panel" onClick={e => e.stopPropagation()}>
        <div className="schema-header">
          <span className="schema-title">QUERY HISTORY</span>
          <div style={{ display: "flex", gap: 8 }}>
            {history.length > 0 && <button className="clear-btn" onClick={onClear}>Clear</button>}
            <button className="icon-btn" onClick={onClose}>✕</button>
          </div>
        </div>
        <div className="schema-body">
          {history.length === 0 && <p style={{ color: "var(--muted)", padding: "16px" }}>No queries yet.</p>}
          {history.map(h => (
            <div key={h.id} className="history-item" onClick={() => { onSelect(h.question); onClose(); }}>
              <div className="history-question">{h.question}</div>
              <div className="history-meta">
                <span>{h.row_count} rows</span>
                <span className="db-tag">📦 {h.database}</span>
                {h.auto_fixed && <span className="badge-fix">AUTO-FIXED</span>}
                <span>{new Date(h.timestamp).toLocaleTimeString()}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Chart ─────────────────────────────────────────────────────────────────────
function ResultChart({ rows }) {
  const safeRows = Array.isArray(rows) ? rows.filter(row => row && typeof row === "object") : [];
  const info = detectChartType(safeRows);
  if (!info) return null;
  const data = safeRows
    .filter(r => Number.isFinite(Number(r?.[info.valueKey])))
    .slice(0, 20)
    .map(r => ({
      name: String(r?.[info.labelKey] ?? "").slice(0, 20),
      value: Number(r?.[info.valueKey])
    }))
    .filter(item => item.name);
  if (data.length < 2) return null;
  return (
    <div className="chart-container">
      <div className="section-label">VISUALIZATION</div>
      <ResponsiveContainer width="100%" height={300}>
        {info.type === "pie" ? (
          <PieChart>
            <Pie data={data} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={110}
              label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}>
              {data.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
            </Pie>
            <Tooltip contentStyle={{ background: "#0d1117", border: "1px solid #30363d", borderRadius: 8 }} />
          </PieChart>
        ) : info.type === "line" ? (
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
            <XAxis dataKey="name" tick={{ fill: "#8b949e", fontSize: 11 }} />
            <YAxis tick={{ fill: "#8b949e", fontSize: 11 }} />
            <Tooltip contentStyle={{ background: "#0d1117", border: "1px solid #30363d", borderRadius: 8 }} />
            <Line type="monotone" dataKey="value" stroke="#00f5d4" strokeWidth={2} dot={{ fill: "#00f5d4", r: 3 }} />
          </LineChart>
        ) : (
          <BarChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
            <XAxis dataKey="name" tick={{ fill: "#8b949e", fontSize: 11 }} />
            <YAxis tick={{ fill: "#8b949e", fontSize: 11 }} />
            <Tooltip contentStyle={{ background: "#0d1117", border: "1px solid #30363d", borderRadius: 8 }} />
            <Bar dataKey="value" radius={[4, 4, 0, 0]}>
              {data.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
            </Bar>
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [question, setQuestion] = useState("");
  const [response, setResponse] = useState(null);
  const [loading, setLoading] = useState(false);
  const [schema, setSchema] = useState([]);
  const [history, setHistory] = useState([]);
  const [connections, setConnections] = useState([]);
  const [activeConnId, setActiveConnId] = useState("");
  const [activeDb, setActiveDb] = useState("");
  const [bookmarks, setBookmarks] = useState(loadBookmarks);
  const [showSchema, setShowSchema] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showDBSwitcher, setShowDBSwitcher] = useState(false);
  const [showBookmarks, setShowBookmarks] = useState(false);
  const [uploadStatus, setUploadStatus] = useState(null);
  const [suggestions, setSuggestions] = useState([]);
  const [activeTab, setActiveTab] = useState("table");
  const [uploading, setUploading] = useState(false);
  const [switchMsg, setSwitchMsg] = useState("");
  const fileRef = useRef();

  const showSwitchMessage = (message) => {
    setSwitchMsg(message);
    setTimeout(() => setSwitchMsg(""), 3000);
  };

  const fetchConnections = useCallback(async () => {
    try {
      const res = await fetchWithTimeout(`${API}/connections`);
      const data = await safeJson(res);
      if (!res.ok) throw new Error(getApiError(data, "Could not load connections."));
      setConnections(Array.isArray(data.connections) ? data.connections : []);
      setActiveConnId(data.active || "");
      setActiveDb(data.active_display || "");
      if (!data.active) setSchema([]);
    } catch (e) { console.error(e); }
  }, []);
  const fetchSchema = useCallback(async () => {
    try {
      const res = await fetchWithTimeout(`${API}/schema`);
      const data = await safeJson(res);
      if (!res.ok) throw new Error(getApiError(data, "Could not load schema."));
      const nextSchema = Array.isArray(data.schema) ? data.schema : [];
      setActiveDb(data.active_db || "");
      await fetchConnections();
      setSchema(nextSchema);
    } catch (e) { console.error(e); }
  }, [fetchConnections]);
  const fetchHistory = useCallback(async () => {
    try {
      const res = await fetchWithTimeout(`${API}/history`);
      const data = await safeJson(res);
      if (!res.ok) throw new Error(getApiError(data, "Could not load history."));
      setHistory(Array.isArray(data.history) ? data.history : []);
    } catch (e) { console.error(e); }
  }, []);

  useEffect(() => {
    const loadId = setTimeout(() => {
      fetchSchema();
      fetchHistory();
      fetchConnections();
    }, 0);
    return () => clearTimeout(loadId);
  }, [fetchSchema, fetchHistory, fetchConnections]);
  const switchDatabase = async (connId) => {
    try {
      const res = await fetchWithTimeout(`${API}/connections/switch`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conn_id: connId })
      });
      const data = await safeJson(res);
      if (!res.ok) throw new Error(getApiError(data, "Database switch failed."));
      if (data.success) {
        setActiveConnId(data.active);
        setActiveDb(data.display);
        showSwitchMessage(`Switched to ${data.display}`);
        await fetchSchema(); setShowDBSwitcher(false);
      } else {
        showSwitchMessage(getApiError(data, "Database switch failed."));
      }
    } catch (e) {
      showSwitchMessage(e.name === "AbortError" ? "Database switch timed out. Please try again." : e.message);
    }
  };
  const handleDbUploaded = async (data) => {
    if (data.connections) setConnections(data.connections);
    if (data.display) setActiveDb(data.display);
    if (data.active || data.conn_id) setActiveConnId(data.active || data.conn_id);
    showSwitchMessage(`Connected to ${data.display || data.message}`);
    await fetchSchema(); setShowDBSwitcher(false);
  };
  const handleConnected = (data) => {
    if (data.connections) setConnections(data.connections);
    if (data.conn_id) {
      switchDatabase(data.conn_id);
    } else {
      fetchConnections();
    }
  };
  const generateSQL = async (q = question) => {
    const trimmed = q.trim();
    if (!trimmed) return;
    const blockedKeyword = hasDangerousKeyword(trimmed);
    if (blockedKeyword) {
      setResponse({ error: `Blocked dangerous operation before sending request: ${blockedKeyword}` });
      setActiveTab("table");
      return;
    }

    setLoading(true); setResponse(null);
    try {
      const res = await fetchWithTimeout(`${API}/generate-sql`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: trimmed }),
      });
      const data = await safeJson(res);
      if (!res.ok) throw new Error(getApiError(data, "Query failed. Please try again."));
      if (data.detail || data.error) throw new Error(getApiError(data));
      const normalized = normalizeQueryResponse(data, trimmed);
      if (!normalized.generated_sql) throw new Error("The backend did not return generated SQL. Please try rephrasing your question.");
      setResponse(normalized); setActiveTab("table"); fetchHistory();
    } catch (e) {
      setResponse({ error: e.name === "AbortError" ? "The request timed out after 30 seconds. Please try a narrower question." : e.message });
    }
    finally { setLoading(false); }
  };
  const handleUpload = async (e) => {
    const file = e.target.files[0]; if (!file) return;
    setUploading(true); setUploadStatus(null); setSuggestions([]);
    const formData = new FormData(); formData.append("file", file);
    // use append mode if same filename was already uploaded this session
    const mode = uploadStatus?.table_name === file.name.replace(/\.[^.]+$/, "").replace(/[^\w]/g, "_").toLowerCase()
      ? "append" : "replace";
    try {
      const res = await fetchWithTimeout(
        `${API}/upload-csv?mode=${mode}`,
        { method: "POST", body: formData },
        UPLOAD_TIMEOUT_MS   // longer timeout for large files
      );
      const data = await safeJson(res);
      if (!res.ok) throw new Error(getApiError(data, "Upload failed."));
      if (data.detail || data.error) throw new Error(getApiError(data, "Upload failed."));
      setUploadStatus(data);
      if (Array.isArray(data.suggestions)) setSuggestions(data.suggestions);
      // The upload response is authoritative for the newly active isolated DB;
      // update the header before schema refresh completes.
      if (data.conn_id) setActiveConnId(data.conn_id);
      if (data.active_db) setActiveDb(data.active_db);
      await fetchSchema();
    } catch (e) {
      setUploadStatus({ error: e.name === "AbortError" ? "Upload timed out. Try a smaller file or check your connection." : e.message });
    }
    finally { setUploading(false); e.target.value = ""; }
  };
  const clearHistory = async () => {
    try {
      await fetchWithTimeout(`${API}/history`, { method: "DELETE" });
    } catch (e) {
      console.error(e);
    }
    setHistory([]);
  };
  const isBookmarked = response ? bookmarks.some(b => b.question === response.question) : false;
  const toggleBookmark = () => {
    if (!response) return;
    let updated;
    if (isBookmarked) {
      updated = bookmarks.filter(b => b.question !== response.question);
    } else {
      updated = [{ id: Date.now(), question: response.question || question, sql: response.generated_sql || "", database: response.active_db || activeDb, savedAt: new Date().toISOString() }, ...bookmarks];
    }
    setBookmarks(updated); saveBookmarks(updated);
  };
  const deleteBookmark = (id) => { const updated = bookmarks.filter(b => b.id !== id); setBookmarks(updated); saveBookmarks(updated); };
  const handleKeyDown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) generateSQL(); };
  const responseRows = Array.isArray(response?.results) ? response.results : [];
  const responseTotalRows = Number.isFinite(Number(response?.total_rows)) ? Number(response.total_rows) : responseRows.length;
  const responseSql = response?.generated_sql || "";
  const responseExplanation = response?.explanation || "";
  const responseActiveDb = response?.active_db || activeDb;
  const chartInfo = detectChartType(responseRows);

  return (
    <div className="app">
      <div className="ambient" />
      <header className="header">
        <div className="logo">
          <span className="logo-icon">⬡</span>
          <span className="logo-text">QueryMind<span className="logo-ai">.ai</span></span>
        </div>
        <div className="header-actions">
          <button className="db-indicator" onClick={() => setShowDBSwitcher(true)}>
            <span className="dot green" />
            <span className="db-indicator-name">{activeDb || "No DB"}</span>
            <span className="db-indicator-arrow">⌄</span>
          </button>
          <button className="hdr-btn" onClick={() => { setShowSchema(true); fetchSchema(); }}>
            <span>🗃</span> Schema {schema.length > 0 && <span className="badge">{schema.length}</span>}
          </button>
          <button className="hdr-btn" onClick={() => setShowBookmarks(true)}>
            <span>★</span> Bookmarks {bookmarks.length > 0 && <span className="badge">{bookmarks.length}</span>}
          </button>
          <button className="hdr-btn" onClick={() => { setShowHistory(true); fetchHistory(); }}>
            <span>🕐</span> History {history.length > 0 && <span className="badge">{history.length}</span>}
          </button>
          <button className="hdr-btn upload-btn" onClick={() => fileRef.current.click()} disabled={uploading}>
            {uploading ? <span className="spinner" /> : <span>⬆</span>}
            {uploading ? "Uploading..." : "Upload CSV / Excel"}
          </button>
          <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls" style={{ display: "none" }} onChange={handleUpload} />
        </div>
      </header>

      <main className="main">
        {switchMsg && <div className="switch-toast"><span className="dot green" /> {switchMsg}</div>}
        <div className="hero">
          <h1 className="hero-title">Ask your database<br /><span className="hero-accent">anything.</span></h1>
          <p className="hero-sub">Natural language → SQL → Insights. Powered by AI.</p>
        </div>
        {uploadStatus && !uploadStatus.error && (
          <div className="upload-success">
            <span className="dot green" />
            <div className="upload-success-body">
              <span>
                <strong>{uploadStatus.table_name}</strong>
                {" — "}
                <span className="upload-mode-badge">{uploadStatus.mode || "replace"}</span>
                {" "}
                {(uploadStatus.rows_added ?? uploadStatus.row_count ?? 0).toLocaleString()} rows added
                {uploadStatus.total_rows != null && uploadStatus.total_rows !== uploadStatus.rows_added &&
                  <span className="upload-total"> ({uploadStatus.total_rows.toLocaleString()} total)</span>
                }
                {uploadStatus.columns?.length > 0 &&
                  <span className="upload-cols">, {uploadStatus.columns.length} columns</span>
                }
              </span>
              {uploadStatus.db_file && (
                <span className="upload-db-tag">📦 {uploadStatus.db_file}</span>
              )}
            </div>
          </div>
        )}
        {uploadStatus?.error && <div className="upload-error">⚠ {uploadStatus.error}</div>}
        {suggestions.length > 0 && (
          <div className="suggestions">
            <div className="section-label">SUGGESTED QUESTIONS</div>
            <div className="suggestion-chips">
              {suggestions.map((s, i) => (
                <button key={i} className="chip" onClick={() => { setQuestion(s); generateSQL(s); }}>{s}</button>
              ))}
            </div>
          </div>
        )}
        <div className="query-box">
          <div className="query-prompt">
            <span className="prompt-symbol">›_</span>
            <textarea className="query-input" placeholder="e.g. Show top 10 states with most cybercrimes in 2022"
              value={question} onChange={e => setQuestion(e.target.value)} onKeyDown={handleKeyDown} rows={3} />
          </div>
          <div className="query-footer">
            <span className="shortcut-hint">Ctrl+Enter to run</span>
            <button className="run-btn" onClick={() => generateSQL()} disabled={loading || !question.trim()}>
              {loading ? <><span className="spinner" /> Generating...</> : <><span>▶</span> Run Query</>}
            </button>
          </div>
        </div>
        {loading && <div className="loading-state"><div className="loading-bar" /><p>Generating SQL & fetching results...</p></div>}
        {response?.error && (
          <div className="error-box">
            <span className="error-icon">⚠</span>
            <div><strong>Query Failed</strong><p>{response.error}</p></div>
          </div>
        )}
        {response && !response.error && (
          <div className="results-section">
            <SQLBlock sql={responseSql} autoFixed={response.auto_fixed} activeDb={responseActiveDb} onBookmark={toggleBookmark} isBookmarked={isBookmarked} />
            {responseExplanation && (
              <div className="explanation-box">
                <span className="exp-icon">✦</span>
                <div>
                  <div className="section-label" style={{ marginBottom: 6 }}>AI INSIGHT</div>
                  <p className="exp-text">{responseExplanation}</p>
                </div>
              </div>
            )}
            <div className="result-tabs">
              <div className="tab-bar">
                <button className={`tab ${activeTab === "table" ? "active" : ""}`} onClick={() => setActiveTab("table")}>
                  Table <span className="tab-count">{responseTotalRows}</span>
                </button>
                {chartInfo && (
                  <button className={`tab ${activeTab === "chart" ? "active" : ""}`} onClick={() => setActiveTab("chart")}>Chart</button>
                )}
                <button className="export-btn" onClick={() => exportToCSV(responseRows)}>⬇ Export CSV</button>
              </div>
              {activeTab === "table" && <DataTable rows={responseRows} />}
              {activeTab === "chart" && <ResultChart rows={responseRows} />}
            </div>
          </div>
        )}
      </main>

      {showSchema && <SchemaPanel schema={schema} activeDb={activeDb} onClose={() => setShowSchema(false)} />}
      {showHistory && <HistoryPanel history={history} onSelect={q => setQuestion(q)} onClear={clearHistory} onClose={() => setShowHistory(false)} />}
      {showBookmarks && <BookmarksPanel bookmarks={bookmarks} onSelect={q => { setQuestion(q); generateSQL(q); }} onDelete={deleteBookmark} onClose={() => setShowBookmarks(false)} />}
      {showDBSwitcher && <DBSwitcherPanel connections={connections} activeConnId={activeConnId} onSwitch={switchDatabase} onUploadDb={handleDbUploaded} onConnected={handleConnected} onRefresh={fetchConnections} onClose={() => setShowDBSwitcher(false)} />}
    </div>
  );
}
