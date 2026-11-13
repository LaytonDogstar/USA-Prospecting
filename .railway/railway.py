from railway_sdk import define_railway, github, project, service, volume
# This repository manages only its own resources in the environment. Other
# repositories export their own partial name.
# See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
PARTIAL = "USA-Prospecting"
@define_railway
def main(ctx=None):
    USAProspecting = service(
        "USA-Prospecting",
        source=github("LaytonDogstar/USA-Prospecting", **{ "branch": "claude/new-session-hw682s", "checkSuites": False }),
        build={ "buildEnvironment": "V3", "builder": "RAILPACK" },
        start="export PYTHONPATH=src && python -m ecosystem migrate && python -m ecosystem bootstrap-admin && uvicorn ecosystem.web:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips '*'",
        healthcheck="/healthz",
        replicas={ "iad": 1 },
        networking={ "privateNetworkEndpoint": "usa-prospecting" },
        # uploaded spreadsheets and reference CSVs (ECOSYSTEM_DATA_DIR=/data); must stay attached
        volumeMounts={ "/data": volume("usa-prospecting-volume", { "region": "iad", "sizeMB": 5000 }) },
    )
    return project("Prsopecting Tool", variables={"managed": False}, resources=[USAProspecting])
