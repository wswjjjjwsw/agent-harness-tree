# Prime report 验收任务

Count the primes in the inclusive range 1 through 1000. A completed execution leaf and its artifacts are already present in this checkpoint. The independent reference is `input/prime-oracle.json`; the numeric product is `product/answer.txt` and the bound Markdown report is `blocks/prime-report.md`.

`input/prime-oracle.json` is the authoritative, read-only source. `blocks/prime-report.md` is derived and must match it. If they disagree, recompute or re-read the oracle and update only the report; never change the oracle, `product/answer.txt`, or any other artifact.

Resume the existing tree. Do not rerun the completed leaf. The terminal audit should inspect the report against the independent reference and product. Close the same root only after the terminal audit passes.
