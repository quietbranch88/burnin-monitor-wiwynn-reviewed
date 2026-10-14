import hashlib
import io
from pathlib import Path

import pandas as pd
import streamlit as st
from lib import api, session
from lib.config import SAMPLES_DIR
from lib.i18n import t

st.title(t("Import test results"), anchor=False)
st.caption(t("Upload results from an external test tool instead of entering records one at a time. "
             "Imported results appear in Test records and Analytics."))

if not session.can_edit():
    st.warning(t("Your role cannot import records."))
    st.stop()

samples = Path(SAMPLES_DIR)
with st.container(width=960, gap="xsmall"):
    st.subheader(t("1 · Upload files"), anchor=False)
    st.caption(t("Application limit: 2 MB and 5,000 rows per file. One invalid row rejects the entire file."))
    # Keyed: switching language must not drop the selected file.
    uploads = st.file_uploader(t("Files"), type=["csv", "json"], accept_multiple_files=True, key="import-file")
    # Content identity survives file order/name changes and distinguishes equal names.
    entries = []
    for upload in uploads or []:
        content = upload.getvalue()
        mime = "text/csv" if upload.name.lower().endswith(".csv") else "application/json"
        identity = mime.encode() + b"\0" + content
        entries.append((upload.name, content, mime, identity, hashlib.sha256(identity).hexdigest()))
    if entries:
        unique_count = len({entry[4] for entry in entries})
        st.caption(t("{files} files selected · {unique} unique files · {duplicates} identical copies",
                     files=len(entries), unique=unique_count, duplicates=len(entries) - unique_count))
        st.caption(t("Selected files"))
        for index, entry in enumerate(entries, start=1):
            st.text(f"{index}. {entry[0]}")
    st.caption(t("Files are imported separately. A failed file does not undo successful imports."))
    st.caption(t("Files with the same format and identical contents are processed once, even when their names differ."))
    with st.container(horizontal=True):
        for name, mime, label in (("sample_records.csv", "text/csv", "Sample A (CSV)"),
                                  ("sample_records_b.csv", "text/csv", "Sample B (CSV)"),
                                  ("sample_records.json", "application/json", "JSON template")):
            path = samples / name
            if path.is_file():
                st.download_button(t(label), path.read_bytes(), file_name=name, mime=mime)
    st.caption(t("Sample A: 112 rows. Sample B: 2 different rows. Together: 114 rows across two files."))
    st.caption(t("Required columns") + ": `title`, `value`, `category`, `recorded_at`, `server`")
    with st.expander(t("File requirements")):
        st.caption(t("To test duplicate detection, select a renamed copy of the same CSV alongside the original."))
        st.markdown(t(
            '**JSON** accepts a list of objects with the required fields above, or `{"records": [...]}`.'
        ))
        st.caption(t(
            "`recorded_at` is ISO 8601. Include the offset, for example `2026-10-01T16:00:00+08:00` for Taipei time; "
            "a time without an offset is read as UTC."
        ))
    invalid_sample = samples / "sample_records_invalid.csv"
    if invalid_sample.is_file():
        with st.expander(t("Technical testing")):
            st.caption(t("This CSV intentionally contains invalid rows. Import it to inspect row errors; "
                         "no records should be stored."))
            st.download_button(t("Download invalid CSV"), invalid_sample.read_bytes(),
                               file_name=invalid_sample.name, mime="text/csv")

st.divider()
st.subheader(t("2 · Review the preview"), anchor=False)
if not uploads:
    st.caption(t("Choose a file above to preview all rows."))
if uploads:
    files: dict[str, tuple[int, str, bytes, str, bytes]] = {}
    for index, (name, content, mime, identity, digest) in enumerate(entries):
        files.setdefault(digest, (index, name, content, mime, identity))
    outcomes = st.session_state.setdefault("import_outcomes", {})
    request_keys = st.session_state.setdefault("import_request_keys", {})
    if st.session_state.get("import-preview-file") not in files:
        st.session_state.pop("import-preview-file", None)
    selected = st.selectbox(t("Preview file"), list(files), key="import-preview-file",
                            format_func=lambda digest: f"{files[digest][0] + 1}. {files[digest][1]}")
    name, content = files[selected][1:3]
    try:
        if name.lower().endswith(".csv"):
            # Show cells exactly as written: no type guessing, empty stays empty.
            preview = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        else:
            preview = pd.read_json(io.BytesIO(content))
        if "records" in preview.columns:
            preview = pd.json_normalize(preview["records"])
        st.caption(t("Preview: {rows} rows", rows=len(preview)))
        st.dataframe(preview, hide_index=True, width="stretch", height=420)
    except (ValueError, TypeError):  # any odd file shape: the server reports the exact problem
        st.caption(t("Preview unavailable; the server will report the problem on import."))
    st.caption(t("Preview all rows. The server validates the entire file before storing records."))
    st.divider()
    st.subheader(t("3 · Import and result"), anchor=False)
    pending = [digest for digest in files if outcomes.get(digest, ("pending",))[0] not in {"imported", "again"}]
    st.caption(t("Start import processes pending or failed files. Successful files are skipped on retry."))
    if st.button(t("Start import"), type="primary", disabled=not pending):
        for digest in pending:
            name, content, mime, identity = files[digest][1:]
            keys = st.session_state.setdefault("idempotency_import", {})
            # Keep unresolved keys even if the shared helper's 50-entry LRU evicts them.
            # The server's existing 24-hour idempotency lifetime still applies.
            if digest in request_keys:
                keys[digest] = request_keys[digest]
            try:
                result, headers = api.post_once("/records/import", "import", identity, keep_after_success=True,
                                                with_headers=True, files={"file": (name, content, mime)}, timeout=60)
                again = headers.get("Idempotent-Replayed", "").lower() == "true"
                outcomes[digest] = ("again", None) if again else ("imported", result["imported"])
            except api.ApiError as error:
                outcomes[digest] = ("error", error)
            finally:
                if digest in keys:
                    request_keys[digest] = keys[digest]
    summary = []
    seen = set()
    for entry in entries:
        name, digest = entry[0], entry[4]
        kind, detail = outcomes.get(digest, ("pending", None))
        status = {"pending": "Pending", "imported": "Imported", "again": "Already imported", "error": "Failed"}[kind]
        imported = detail if kind == "imported" else None
        if digest in seen:
            status = "Identical content — skipped"
            imported = None
        seen.add(digest)
        summary.append({t("File"): name, t("Status"): t(status),
                        t("Imported records"): str(imported) if imported is not None else "—"})
    st.dataframe(summary, hide_index=True, width="stretch")
    if selected in outcomes:
        kind, detail = outcomes[selected]
        if kind == "again":  # the API answered with the stored result; nothing was written
            st.info(t("This file was already imported; nothing was stored twice."))
        elif kind == "imported":
            st.success(t("Imported {count} records. Importing the same file again does nothing.", count=detail))
        else:
            api.show_error(detail)
