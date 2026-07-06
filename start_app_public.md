cd C:\Users\EKGAH\Documents\project\ops-monitor\backend
$env:OPS_MONITOR_RELOAD = "0"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001


url:-
http://10.29.245.25:8001

