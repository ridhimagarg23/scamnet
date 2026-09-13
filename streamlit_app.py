"""
streamlit_app.py
================
Static Streamlit mock-up of the TraceAI dashboard.

NOTE: this is a UI PROTOTYPE / wireframe, not the live application.
It renders a fixed example scenario (banking phishing, HIGH risk) and
only echoes typed messages into the chat column - it does NOT call
the agents or the API.

The production UI is the Next.js dashboard in ``frontend/`` wired to
``backend/api.py``. Run this file only for quick layout previews:

    streamlit run streamlit_app.py
"""

import streamlit as st

# -------------------------
# Page Config
# -------------------------

st.set_page_config(
    page_title="TraceAI",
    page_icon="🛡️",
    layout="wide"
)

# -------------------------
# Session State
# -------------------------
# Streamlit reruns the script on every interaction; st.session_state
# is the only place chat lines survive between reruns.

if "messages" not in st.session_state:
    st.session_state.messages = []

# -------------------------
# Sidebar
# -------------------------

with st.sidebar:

    st.title("🛡 TraceAI")

    st.caption("AI Security Layer")

    st.divider()

    # NOTE: buttons below are visual placeholders only.
    st.button(
        "➕ New Investigation",
        use_container_width=True
    )

    st.button(
        "📜 History",
        use_container_width=True
    )

    st.button(
        "📄 Reports",
        use_container_width=True
    )

    st.button(
        "🔄 Reset",
        use_container_width=True
    )

# -------------------------
# Main Layout (3 columns: persona | chat | overview)
# -------------------------

left, center, right = st.columns(
    [1.1, 3, 1.3]
)

# =========================
# LEFT - Active persona card
# =========================

with left:

    st.subheader("👤 Active Persona")

    st.info(
        """
**Working Professional**

🌍 Hinglish

💬 Polite

🟢 Active
"""
    )

# =========================
# CENTER - Chat column
# =========================

with center:

    st.title("💬 Live Investigation")

    st.caption(
        "Conversation between scammer and TraceAI Persona"
    )

    st.divider()

    # Render the stored transcript.
    for message in st.session_state.messages:

        with st.chat_message(
            message["role"]
        ):

            st.markdown(
                message["content"]
            )

    # Chat input: appends the analyst-pasted scammer line.
    user_msg = st.chat_input(
        "Paste scammer's latest message..."
    )

    if user_msg:

        st.session_state.messages.append(
            {
                "role": "user",
                "content": user_msg
            }
        )

        st.rerun()

# =========================
# RIGHT - Static overview (fixed demo values)
# =========================

with right:

    st.subheader("🛡 Investigation Hub")

    # Demo values: the live app computes these from real evidence.
    st.error("🔴 HIGH RISK")

    st.metric(
        "Threat",
        "Banking Phishing"
    )

    st.metric(
        "Confidence",
        "95%"
    )

    st.divider()

    st.write("### Investigation Progress")

    # Sample progress: 45% demo fill.
    st.progress(45)

    st.write("✅ Threat Detected")

    st.write("✅ IOC Extracted")

    st.write("🟡 Website Investigation")

    st.write("⚪ Contact Collection")

    st.write("⚪ Investigation Complete")

    st.divider()

    st.write("### Evidence")

    st.success("🌐 URL")

    st.success("📧 Email")

    st.success("📱 Phone")

    st.success("💳 UPI")
