from groq import Groq
from schema import get_database_schema
from config import GROQ_API_KEY, GROQ_MODEL_PRIMARY, GROQ_MODEL_FALLBACK

import re
import time

# =========================
# CLIENT
# =========================
client = Groq(api_key=GROQ_API_KEY)

# =========================
# MEMORY (lightweight session context)
# =========================
SESSION_MEMORY = {
    "last_question": None,
    "last_sql": None,
    "last_tables": None
}

# =========================
# SCHEMA CACHE
# =========================
_SCHEMA_CACHE = None


def get_cached_schema():
    global _SCHEMA_CACHE

    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = get_database_schema()

    return _SCHEMA_CACHE


def clear_schema_cache():
    global _SCHEMA_CACHE
    _SCHEMA_CACHE = None


# =========================
# SQL SAFETY LAYER
# =========================
FORBIDDEN_SQL = [
    "DROP",
    "DELETE",
    "UPDATE",
    "INSERT",
    "ALTER",
    "TRUNCATE",
    "CREATE",
    "REPLACE",
    "GRANT",
    "REVOKE"
]


def is_safe_sql(sql: str) -> bool:

    if not sql:
        return False

    sql_upper = sql.upper().strip()

    # must start with SELECT
    if not sql_upper.startswith("SELECT"):
        return False

    # block dangerous keywords
    for word in FORBIDDEN_SQL:
        if re.search(rf"\b{word}\b", sql_upper):
            return False

    return True


# =========================
# SQL CLEANER
# =========================
def clean_sql(raw: str):

    if not raw:
        return ""

    raw = raw.replace("```sql", "")
    raw = raw.replace("```", "")
    raw = raw.strip()

    # Extract first SELECT statement
    match = re.search(
        r"(SELECT[\s\S]*?;)",
        raw,
        re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

    # fallback if semicolon missing
    match = re.search(
        r"(SELECT[\s\S]*)",
        raw,
        re.IGNORECASE
    )

    if match:
        return match.group(1).strip() + ";"

    return ""


# =========================
# GROQ CALL (retry + fallback)
# =========================
def call_llm(prompt, retries=2):

    last_error = None

    for attempt in range(retries):

        try:
            response = client.chat.completions.create(
                model=GROQ_MODEL_PRIMARY,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )

            return response.choices[0].message.content

        except Exception as e:
            last_error = str(e)
            time.sleep(0.5)

    # fallback model
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL_FALLBACK,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        return response.choices[0].message.content

    except Exception:
        return ""


# =========================
# BI ANALYST PROMPT ENGINE
# =========================
def build_prompt(question, schema):

    memory_context = ""

    if SESSION_MEMORY["last_question"]:
        memory_context = f"""
Previous Question: {SESSION_MEMORY['last_question']}
Previous SQL: {SESSION_MEMORY['last_sql']}
"""

    return f"""
You are a SENIOR BI DATA ANALYST and SQL EXPERT.

TASK:
Convert natural language into a VALID SQLite SELECT query.

HARD RULES:
- ONLY SELECT queries allowed
- No explanations
- No markdown
- Must end with semicolon
- Use ONLY given schema

MEMORY CONTEXT:
{memory_context}

DATABASE SCHEMA:
{schema}

USER QUESTION:
{question}

Return ONLY SQL:
"""


# =========================
# SQL GENERATION CORE
# =========================
def generate_sql(user_prompt):

    # block dangerous intent BEFORE LLM
    blocked_words = [
        "delete",
        "drop",
        "truncate",
        "update",
        "insert",
        "alter",
        "create",
        "replace"
    ]

    lower_prompt = user_prompt.lower()

    for word in blocked_words:
     if re.search(rf"\b{word}\b", lower_prompt):
        raise ValueError(f"Blocked dangerous operation: {word}")

    schema = get_cached_schema()

    prompt = build_prompt(user_prompt, schema)

    raw = call_llm(prompt)

    sql = clean_sql(raw)

    # safety check
    if not is_safe_sql(sql):
        raise ValueError("Unsafe SQL detected")

    # store memory
    SESSION_MEMORY["last_question"] = user_prompt
    SESSION_MEMORY["last_sql"] = sql

    return sql


# =========================
# AUTO FIX ENGINE
# =========================
def fix_sql(question, bad_sql, error, schema):

    prompt = f"""
You are a SQL debugging expert.

Fix the SQL query.

RULES:
- ONLY SELECT query
- No explanation
- Must work in SQLite

SCHEMA:
{schema}

QUESTION:
{question}

BROKEN SQL:
{bad_sql}

ERROR:
{error}

FIXED SQL:
"""

    raw = call_llm(prompt)

    sql = clean_sql(raw)

    if not is_safe_sql(sql):
        raise ValueError("Unsafe SQL detected")

    return sql


# =========================
# INSIGHTS ENGINE (BI MODE)
# =========================
def explain_results(question, sql, results):

    prompt = f"""
You are a BI Analyst.

Explain insights from this data in simple business terms.

RULES:
- 2 to 4 sentences
- No SQL mention
- Focus on trends & meaning

Question:
{question}

Results sample:
{results[:5]}
"""

    return call_llm(prompt)


# =========================
# FOLLOW-UP QUESTION HANDLER
# =========================
def enhance_with_context(question):

    if SESSION_MEMORY["last_question"]:

        return f"""
Follow-up question:
{question}

Context:
Previous question: {SESSION_MEMORY['last_question']}
Previous SQL: {SESSION_MEMORY['last_sql']}
"""

    return question


# =========================
# MAIN ENTRY POINT (used by api.py)
# =========================
def process_nl_to_sql(question):

    question = enhance_with_context(question)

    sql = generate_sql(question)

    return {
        "sql": sql,
        "memory": SESSION_MEMORY
    }