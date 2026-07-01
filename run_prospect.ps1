$r = Invoke-RestMethod -Uri "http://localhost:8001/run-prospect-intelligence" -Method POST -ContentType "application/json" -Body '{"input_file":"data/prospects/input/prospects.xlsx","concurrency":2}' -TimeoutSec 7200
$r | ConvertTo-Json -Depth 5 | Out-File "c:\Users\SSLTP12090\ai-harvest-agent\prospect_result.json" -Encoding utf8
"DONE" | Out-File "c:\Users\SSLTP12090\ai-harvest-agent\prospect_job_status.txt" -Encoding utf8
