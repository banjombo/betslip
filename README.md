# betslip1-backend

FastAPI backend for betslip1 iMessage extension.

## Local dev

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ODDS_API_KEY=YOUR_KEY
uvicorn main:app --reload --port 8000
