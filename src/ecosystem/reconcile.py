"""Count reconciliation between the source sheets and the database (Phase 1 acceptance)."""
from collections import Counter

from sqlalchemy import select, func

from . import models as m
from .store import count


def report(s) -> dict:
    orgs = Counter(dict(s.execute(select(m.Organisation.lifecycle, func.count()).group_by(m.Organisation.lifecycle)).all()))
    kinds = Counter(dict(s.execute(select(m.Organisation.kind, func.count()).where(m.Organisation.lifecycle != "merged")
                                   .group_by(m.Organisation.kind)).all()))
    roles = Counter(dict(s.execute(select(m.OrgRole.role, func.count()).group_by(m.OrgRole.role)).all()))
    reviews = Counter(dict(s.execute(select(m.ReviewItem.kind, func.count()).where(m.ReviewItem.status == "open")
                                     .group_by(m.ReviewItem.kind)).all()))
    ours = Counter(dict(s.execute(select(m.OurRelationship.status, func.count()).group_by(m.OurRelationship.status)).all()))
    sheet_rows = Counter()
    for sheet, n in s.execute(select(m.Source.sheet, func.count(func.distinct(m.Source.row_number)))
                              .where(m.Source.sheet.is_not(None)).group_by(m.Source.sheet)):
        sheet_rows[sheet] = n
    universe_status = Counter()
    for v, in s.execute(select(m.Claim.value).where(m.Claim.attribute == "sheet_universe_status")):
        universe_status["screened out" if str(v).startswith("screened out") else v] += 1
    return {
        "rows_imported_per_sheet": dict(sheet_rows),
        "universe_status": dict(universe_status),
        "organisations_by_lifecycle": dict(orgs),
        "organisations_by_kind": dict(kinds),
        "roles": dict(roles),
        "brands": count(s, m.Brand),
        "domains": count(s, m.Domain),
        "claims": count(s, m.Claim),
        "sources": count(s, m.Source),
        "relationships": count(s, m.Relationship),
        "metric_snapshots": count(s, m.MetricSnapshot),
        "compliance_flags": count(s, m.ComplianceFlag),
        "entity_states": count(s, m.EntityState),
        "our_relationships_by_status": dict(ours),
        "open_review_items": dict(reviews),
        "import_files": [
            {"file": f.file_name, "kind": f.kind, "counts": f.row_counts}
            for f in s.scalars(select(m.ImportFile).order_by(m.ImportFile.id))
        ],
    }


def to_markdown(r: dict) -> str:
    lines = ["# Import reconciliation", ""]
    for k, v in r.items():
        if isinstance(v, dict):
            lines.append(f"## {k.replace('_', ' ')}")
            lines += [f"- {kk}: {vv}" for kk, vv in sorted(v.items(), key=lambda x: str(x[0]))]
            lines.append("")
        elif isinstance(v, list):
            lines.append(f"## {k.replace('_', ' ')}")
            lines += [f"- {x['file']} ({x['kind']}): {x['counts']}" for x in v]
            lines.append("")
        else:
            lines.append(f"- **{k.replace('_', ' ')}**: {v}")
    return "\n".join(lines)
