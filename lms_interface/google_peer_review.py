"""Google Forms front end for peer-review calibration workspaces."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/forms.body", "https://www.googleapis.com/auth/forms.responses.readonly"]
log = logging.getLogger(__name__)


def _services(client_secret: Path, token_path: Path, port: int):
    creds = Credentials.from_authorized_user_file(token_path, SCOPES) if token_path.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        logging.getLogger("google_auth_oauthlib.flow").setLevel(logging.WARNING)
        creds = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES).run_local_server(port=port, open_browser=False)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds), build("forms", "v1", credentials=creds)


def create_forms(work_dir: Path, client_secret: Path, token_path: Path, port: int = 8765) -> Path:
    config = yaml.safe_load((work_dir / "peer-review-workspace.yaml").read_text())
    rubric = yaml.safe_load(Path(config["rubric_config_path"]).read_text())["rubric"]
    drive, forms = _services(client_secret, token_path, port)
    manifest = work_dir / "google-peer-review-forms.json"
    if manifest.exists():
        existing = json.loads(manifest.read_text())
        folder_id = existing["folder"]["id"]
        for entry in existing["forms"]:
            drive.files().update(
                fileId=entry["form_id"],
                addParents=folder_id,
                body={"name": f"Peer review: {entry['pdf_id']}"},
                fields="id,webViewLink",
            ).execute()
        return manifest
    folder = drive.files().create(body={"name": "Peer-review calibration PDFs", "mimeType": "application/vnd.google-apps.folder"}, fields="id,webViewLink").execute()
    with (work_dir / config["files"]["professor_review"]).open(encoding="utf-8-sig", newline="") as file:
        reviews = list(csv.DictReader(file))
    entries = []
    labels = rubric["likert_scale"]["labels"]
    for review in reviews:
        pdf = work_dir / review["pdf_path"]
        uploaded = drive.files().create(body={"name": pdf.name, "parents": [folder["id"]]}, media_body=MediaFileUpload(str(pdf), mimetype="application/pdf"), fields="id,webViewLink").execute()
        form = forms.forms().create(body={"info": {"title": f"Peer review: {review['pdf_id']}"}}).execute()
        drive.files().update(
            fileId=form["formId"],
            addParents=folder["id"],
            body={"name": f"Peer review: {review['pdf_id']}"},
            fields="id",
        ).execute()
        requests: list[dict[str, Any]] = [{"updateFormInfo": {"info": {"description": f"Open the PDF in another tab: {uploaded['webViewLink']}"}, "updateMask": "description"}}]
        rubric_ids: list[str] = []
        index = 0
        for section in rubric.get("sections") or []:
            for question in section.get("questions") or []:
                rubric_ids.append(str(question["id"]))
                q: dict[str, Any] = {"required": question["type"] == "likert"}
                if question["type"] == "likert":
                    q["choiceQuestion"] = {"type": "RADIO", "options": [{"value": str(labels.get(str(i), labels.get(i)))} for i in range(1, 5)]}
                else:
                    q["textQuestion"] = {"paragraph": True}
                requests.append({"createItem": {"item": {"title": str(question["prompt"]), "questionItem": {"question": q}}, "location": {"index": index}}})
                index += 1
        forms.forms().batchUpdate(formId=form["formId"], body={"requests": requests}).execute()
        detail = forms.forms().get(formId=form["formId"]).execute()
        question_ids = [item["questionItem"]["question"]["questionId"] for item in detail.get("items", [])]
        entries.append({"pdf_id": review["pdf_id"], "form_id": form["formId"], "responder_uri": detail.get("responderUri"), "questions": dict(zip(rubric_ids, question_ids, strict=True)), "likert_labels": {str(labels.get(str(i), labels.get(i))): str(i) for i in range(1, 5)}})
    manifest.write_text(json.dumps({"folder": folder, "forms": entries}, indent=2), encoding="utf-8")
    return manifest


def import_forms(work_dir: Path, client_secret: Path, token_path: Path, port: int = 8765) -> int:
    _, forms = _services(client_secret, token_path, port)
    manifest = json.loads((work_dir / "google-peer-review-forms.json").read_text())
    config = yaml.safe_load((work_dir / "peer-review-workspace.yaml").read_text())
    path = work_dir / config["files"]["professor_review"]
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    by_pdf = {row["pdf_id"]: row for row in rows}
    completed = 0
    for entry in manifest["forms"]:
        responses = forms.forms().responses().list(formId=entry["form_id"]).execute().get("responses", [])
        if not responses:
            continue
        completed += 1
        answers = responses[-1].get("answers", {})
        for field, question_id in entry["questions"].items():
            values = answers.get(question_id, {}).get("textAnswers", {}).get("answers", [])
            if values:
                value = values[0].get("value", "")
                by_pdf[entry["pdf_id"]][field] = entry["likert_labels"].get(value, value)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader(); writer.writerows(rows)
    return completed
