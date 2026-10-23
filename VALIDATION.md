# Local validation — 23 October 2026

- Python unittest: 12 tests passed.
- Offline CLI demo: exit 0; one fixture candidate, modelled margin GBP 25.20.
- Restart test: SQLite duplicate claim survived closing and reopening a real database.
- All provider interactions in tests were mocked. No API credentials used, messages sent, accounts accessed or deployments performed.
- Initial sandbox run could not open the Windows temporary database directory; the same test suite passed outside that sandbox with permission.
- V1 source unavailable; no claim of source-level compatibility or V1 data migration.
- Render YAML supplied from the documented Blueprint structure; not validated by the Render service or deployed.

Run from project root:

```text
python -m unittest discover -s tests -v
python -m deal_hunter.app --demo
```
