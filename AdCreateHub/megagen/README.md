# ⚡ MegaGen — mega bulk generator

Generate a whole numbered list of prompts in one press. Images and videos, via
Google Vertex AI (Nano Banana + Veo/Omni) on your $300 free trial.

## Install (once)

```
pip install -r requirements.txt
```

## Run

```
python run_megagen.py
```

Your browser opens at http://127.0.0.1:7861 — keep the console window open.
Add `--no-browser` to skip the auto-open.

## First time here?

Click **Guide** in the top bar (or ⚙ Settings → it opens by itself) for the
5-minute Google Cloud setup: Project ID, enable the API, `gcloud auth
application-default login`.

Generated media lives in `output/<Project>/`. Deleted items go to
`output/.trash` and are recoverable from ⚙ Settings → Trash.
