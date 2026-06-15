
import sys, os, json
sys.path.insert(0, sys.argv[1])
from corpus_tool import export_config as ec
from corpus_tool.database import init_db, ensure_default_rules
init_db()
ensure_default_rules()
cfg, _ = ec.load_config("default")
print(json.dumps({
    "format": cfg.format,
    "summary": cfg.include_review_summary,
    "status": cfg.field_policies["status"],
    "source_file": cfg.field_policies["source_file"],
    "rule_version": cfg.field_policies["rule_version"],
    "final": cfg.field_policies["final_conclusion"],
    "sample_batch": cfg.field_policies["sample_batch"],
}, ensure_ascii=False))
