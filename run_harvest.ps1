try {
    $r = Invoke-RestMethod -Uri "http://localhost:8001/run-harvest-agent" -Method POST -ContentType "application/json" -Body '{}' -TimeoutSec 7200
    $r | ConvertTo-Json -Depth 5 | Out-File "c:\Users\SSLTP12090\ai-harvest-agent\harvest_result.json" -Encoding utf8
    "DONE" | Out-File "c:\Users\SSLTP12090\ai-harvest-agent\harvest_job_status.txt" -Encoding utf8
} catch {
    "FAILED: $_" | Out-File "c:\Users\SSLTP12090\ai-harvest-agent\harvest_job_status.txt" -Encoding utf8
}
