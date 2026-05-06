import streamlit as st
import sqlite3
import pandas as pd
import os
import json

from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI

DB_PATH = "data/resources.db"
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


# ---------------- AI INTERPRET ----------------
def interpret_query_with_ai(user_query):
    if not user_query.strip():
        return {"city": "", "category": "", "keyword": ""}

    prompt = f"""
Extract into JSON:
{{"city":"","category":"","keyword":""}}

Rules:
- Return valid JSON only.
- Possible cities include: Kansas City, St. Louis
- Possible categories include: Housing, Mental Health, Food Support, Legal Aid, Crisis Support

User: {user_query}
"""

    try:
        response = client.responses.create(
            model="gpt-5.2",
            input=prompt
        )

        parsed = json.loads(response.output_text.strip())

        return {
            "city": parsed.get("city", "").strip(),
            "category": parsed.get("category", "").strip(),
            "keyword": parsed.get("keyword", "").strip()
        }

    except Exception as e:
        st.error(f"AI interpretation error: {e}")
        return {"city": "", "category": "", "keyword": user_query}


# ---------------- DB ----------------
def get_connection():
    return sqlite3.connect(DB_PATH)


def load_filter_options():
    conn = get_connection()

    cities = pd.read_sql_query("SELECT * FROM cities ORDER BY city_name", conn)
    categories = pd.read_sql_query("SELECT * FROM categories ORDER BY category_name", conn)

    conn.close()
    return cities, categories


def search_services(city_id=None, category_id=None, keyword=""):
    conn = get_connection()

    query = """
    SELECT s.*, o.org_name, o.phone, o.website, c.city_name, cat.category_name
    FROM services s
    JOIN organizations o ON s.org_id = o.org_id
    JOIN cities c ON s.city_id = c.city_id
    JOIN categories cat ON s.category_id = cat.category_id
    WHERE 1=1
    """

    params = []

    if city_id:
        query += " AND s.city_id = ?"
        params.append(city_id)

    if category_id:
        query += " AND s.category_id = ?"
        params.append(category_id)

    if keyword.strip():
        query += """
        AND (
            s.service_name LIKE ?
            OR s.service_description LIKE ?
            OR o.org_name LIKE ?
            OR s.eligibility LIKE ?
        )
        """
        k = f"%{keyword}%"
        params.extend([k, k, k, k])

    query += " ORDER BY c.city_name, cat.category_name, s.service_name"

    results = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return results


def load_pending_submissions():
    conn = get_connection()

    query = """
    SELECT *
    FROM submitted_resources
    WHERE LOWER(TRIM(status)) = 'pending'
    ORDER BY submission_id DESC
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    return df


def submit_resource(
    org_name,
    service_name,
    city_name,
    category_name,
    service_description,
    eligibility,
    address,
    phone,
    website
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO submitted_resources (
            org_name,
            service_name,
            city_name,
            category_name,
            service_description,
            eligibility,
            address,
            phone,
            website,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending')
    """, (
        org_name,
        service_name,
        city_name,
        category_name,
        service_description,
        eligibility,
        address,
        phone,
        website
    ))

    conn.commit()
    conn.close()


def update_submission_status(submission_id, status):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE submitted_resources
        SET status = ?
        WHERE submission_id = ?
    """, (status, submission_id))

    conn.commit()
    conn.close()


def approve_submission_to_services(submission_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT org_name, service_name, city_name, category_name,
               service_description, eligibility, address, phone, website
        FROM submitted_resources
        WHERE submission_id = ?
    """, (submission_id,))

    row = cursor.fetchone()

    if not row:
        conn.close()
        return "Submission not found."

    (
        org_name,
        service_name,
        city_name,
        category_name,
        description,
        eligibility,
        address,
        phone,
        website
    ) = row

    cursor.execute("""
        SELECT city_id
        FROM cities
        WHERE city_name = ?
    """, (city_name,))
    city_row = cursor.fetchone()

    if not city_row:
        conn.close()
        return f"City '{city_name}' was not found. Submission was not approved."

    city_id = city_row[0]

    cursor.execute("""
        SELECT category_id
        FROM categories
        WHERE category_name = ?
    """, (category_name,))
    category_row = cursor.fetchone()

    if not category_row:
        conn.close()
        return f"Category '{category_name}' was not found. Submission was not approved."

    category_id = category_row[0]

    cursor.execute("""
        SELECT org_id
        FROM organizations
        WHERE LOWER(TRIM(org_name)) = LOWER(TRIM(?))
    """, (org_name,))
    org = cursor.fetchone()

    if org:
        org_id = org[0]
    else:
        cursor.execute("""
            INSERT INTO organizations (org_name, phone, website)
            VALUES (?, ?, ?)
        """, (org_name, phone, website))
        org_id = cursor.lastrowid

    cursor.execute("""
        SELECT service_id
        FROM services
        WHERE LOWER(TRIM(service_name)) = LOWER(TRIM(?))
          AND org_id = ?
          AND city_id = ?
          AND category_id = ?
    """, (service_name, org_id, city_id, category_id))

    existing_service = cursor.fetchone()

    if existing_service:
        cursor.execute("""
            UPDATE submitted_resources
            SET status = 'Approved - Duplicate Not Added'
            WHERE submission_id = ?
        """, (submission_id,))

        conn.commit()
        conn.close()

        return "Duplicate service found. Submission approved, but not added again."

    cursor.execute("""
        INSERT INTO services (
            service_name,
            org_id,
            city_id,
            category_id,
            service_description,
            eligibility,
            address
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        service_name,
        org_id,
        city_id,
        category_id,
        description,
        eligibility,
        address
    ))

    cursor.execute("""
        UPDATE submitted_resources
        SET status = 'Approved'
        WHERE submission_id = ?
    """, (submission_id,))

    conn.commit()
    conn.close()

    return "Submission approved and added to services."


# ---------------- AI ANSWER ----------------
def generate_ai_answer(question, df):
    if not question.strip():
        return "Ask a question first so I can explain the results."

    if df.empty:
        return "No resources found."

    context = ""

    for _, row in df.iterrows():
        context += f"""
Service: {row['service_name']}
Organization: {row['org_name']}
City: {row['city_name']}
Category: {row['category_name']}
Description: {row['service_description']}
Eligibility: {row['eligibility']}
Address: {row['address']}
Phone: {row['phone']}
Website: {row['website']}
---
"""

    prompt = f"""
You are a helpful assistant for an LGBTQ youth resource finder.

Use ONLY the resource data provided below.
Do not invent services, phone numbers, addresses, eligibility rules, or websites.

User question:
{question}

Available resources:
{context}

Answer simply and helpfully. Recommend contacting the organization directly to confirm details.
"""

    try:
        response = client.responses.create(
            model="gpt-5.2",
            input=prompt
        )

        return response.output_text.strip()

    except Exception as e:
        return f"AI answer error: {e}"


# ---------------- RESET ----------------
def reset_filters():
    st.session_state["selected_city"] = "All"
    st.session_state["selected_category"] = "All"
    st.session_state["keyword_search"] = ""


# ---------------- APP ----------------
st.set_page_config(layout="wide")

st.title("🏳️‍🌈 LGBTQ Youth Resource Finder")
st.caption("Search and explore verified LGBTQ youth support resources.")

cities_df, categories_df = load_filter_options()

city_options = ["All"] + cities_df["city_name"].tolist()
category_options = ["All"] + categories_df["category_name"].tolist()

if "selected_city" not in st.session_state:
    st.session_state["selected_city"] = "All"

if "selected_category" not in st.session_state:
    st.session_state["selected_category"] = "All"

if "keyword_search" not in st.session_state:
    st.session_state["keyword_search"] = ""


search_tab, submit_tab, admin_tab = st.tabs([
    "Search",
    "Submit Resource",
    "Admin Review"
])


# ---------------- SEARCH TAB ----------------
with search_tab:

    st.subheader("AI Search")

    ai_query = st.text_input(
        "Describe what you're looking for",
        key="ai_query"
    )

    if st.button("Interpret with AI", key="interpret_ai_button"):
        ai = interpret_query_with_ai(ai_query)

        if ai["city"] in city_options:
            st.session_state["selected_city"] = ai["city"]

        if ai["category"] in category_options:
            st.session_state["selected_category"] = ai["category"]

        st.session_state["keyword_search"] = ai["keyword"]

        st.rerun()

    col1, col2, col3 = st.columns(3)

    with col1:
        selected_city = st.selectbox("City", city_options, key="selected_city")

    with col2:
        selected_category = st.selectbox("Category", category_options, key="selected_category")

    with col3:
        keyword = st.text_input("Keyword", key="keyword_search")

    st.button("Reset", on_click=reset_filters, key="reset_filters_button")

    city_id = None
    category_id = None

    if selected_city != "All":
        city_match = cities_df[cities_df.city_name == selected_city]
        if not city_match.empty:
            city_id = int(city_match.city_id.iloc[0])

    if selected_category != "All":
        category_match = categories_df[categories_df.category_name == selected_category]
        if not category_match.empty:
            category_id = int(category_match.category_id.iloc[0])

    results = search_services(city_id, category_id, keyword)

    st.subheader(f"Results ({len(results)})")

    if results.empty:
        st.warning("No matching services found.")
    else:
        for i, (_, row) in enumerate(results.iterrows()):
            label = f"{row['service_name']} — {row['org_name']}"

            if i == 0:
                label = "⭐ Recommended: " + label

            with st.expander(label):
                st.markdown(f"**City:** {row['city_name']}")
                st.markdown(f"**Category:** {row['category_name']}")
                st.markdown(f"**Description:** {row['service_description']}")
                st.markdown(f"**Eligibility:** {row['eligibility']}")
                st.markdown(f"**Address:** {row['address']}")
                st.markdown(f"**Phone:** {row['phone']}")
                st.markdown(f"**Website:** [Visit Website]({row['website']})")

    st.divider()

    st.subheader("AI Answer")

    q = st.text_input("Ask a follow-up", key="ai_followup")

    if st.button("Generate Answer", key="generate_answer_button"):
        st.write(generate_ai_answer(q, results))


# ---------------- SUBMIT TAB ----------------
with submit_tab:
    st.subheader("Submit a New Resource")

    st.caption("Submitted resources are saved for review before being added to the public resource list.")

    with st.form("resource_submission_form_tab"):
        submitted_org_name = st.text_input("Organization Name", key="submit_org_name")
        submitted_service_name = st.text_input("Service Name", key="submit_service_name")
        submitted_city_name = st.selectbox("City", city_options, key="submit_city")
        submitted_category_name = st.selectbox("Category", category_options, key="submit_category")
        submitted_description = st.text_area("Service Description", key="submit_description")
        submitted_eligibility = st.text_input("Eligibility", key="submit_eligibility")
        submitted_address = st.text_input("Address", key="submit_address")
        submitted_phone = st.text_input("Phone", key="submit_phone")
        submitted_website = st.text_input("Website", key="submit_website")

        submitted = st.form_submit_button("Submit Resource for Review")

        if submitted:
            if (
                submitted_org_name
                and submitted_service_name
                and submitted_city_name != "All"
                and submitted_category_name != "All"
            ):
                submit_resource(
                    submitted_org_name,
                    submitted_service_name,
                    submitted_city_name,
                    submitted_category_name,
                    submitted_description,
                    submitted_eligibility,
                    submitted_address,
                    submitted_phone,
                    submitted_website
                )

                st.success("Resource submitted for review.")
            else:
                st.error("Please complete Organization Name, Service Name, City, and Category.")


# ---------------- ADMIN TAB ----------------
with admin_tab:
    st.subheader("Admin Review Panel")

    st.caption("Review submitted resources before they are added to the public resource list.")

    admin_code = st.text_input("Admin Code", type="password", key="admin_code_input")

    if admin_code == "admin123":
        st.success("Admin access granted")

        pending_submissions = load_pending_submissions()

        st.write(f"Pending submissions: {len(pending_submissions)}")

        if pending_submissions.empty:
            st.info("No pending submissions.")
        else:
            for _, row in pending_submissions.iterrows():
                with st.expander(f"{row['service_name']} — {row['org_name']}"):
                    st.markdown(f"**Submission ID:** {row['submission_id']}")
                    st.markdown(f"**City:** {row['city_name']}")
                    st.markdown(f"**Category:** {row['category_name']}")
                    st.markdown(f"**Description:** {row['service_description']}")
                    st.markdown(f"**Eligibility:** {row['eligibility']}")
                    st.markdown(f"**Address:** {row['address']}")
                    st.markdown(f"**Phone:** {row['phone']}")
                    st.markdown(f"**Website:** {row['website']}")
                    st.markdown(f"**Status:** {row['status']}")

                    col_approve, col_reject = st.columns(2)

                    with col_approve:
                        if st.button("Approve", key=f"approve_{row['submission_id']}"):
                            approval_message = approve_submission_to_services(row["submission_id"])
                            st.success(approval_message)
                            st.rerun()

                    with col_reject:
                        if st.button("Reject", key=f"reject_{row['submission_id']}"):
                            update_submission_status(row["submission_id"], "Rejected")
                            st.warning("Submission rejected.")
                            st.rerun()

    elif admin_code:
        st.error("Invalid admin code.")