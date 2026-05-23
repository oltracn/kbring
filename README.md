# kbring

> **AI Agent**: Read [`kbring.skill`](./kbring.skill) for full usage instructions before running any scripts.

Knowledge base exporters for **DingTalk Docs** and **Yuque** — downloads entire knowledge bases as structured Markdown ZIP archives.

## Scripts

| Script | Platform | URL Pattern |
|---|---|---|
| `dingtalk_kb_exporter.py` | DingTalk Docs | `*alidocs.dingtalk.com*` |
| `yuque_kb_exporter.py` | Yuque | `*yuque.com*` |

## Quick Start

```bash
pip install requests

# DingTalk
python3 dingtalk_kb_exporter.py "<DINGTALK_KB_URL>"

# Yuque
python3 yuque_kb_exporter.py "<YUQUE_KB_URL>"
```

See [`kbring.skill`](./kbring.skill) for detailed options and programmatic usage.
