# 2026-09-01 Best Buy TV crawler tests

## 2026-09-01 14:38 KST - promotion-only recovery validation failure

- Target: Best Buy TV, promotion-only recovery, steps 05/07/08/09/10, run root `C:\samsung_dx_sea\bestbuy\new\bestbuy\data\tv\20260831_3`
- Command: `run_bestbuy_promotion_recovery.bat b_20260831_215613`
- Conditions: batch `b_20260831_215613`; browser DOM promotion collection; visible browser; direct browser GraphQL detail/review/availability; no paid proxy; no S3 sync; DB row-upsert mode was configured but DB load was not reached
- Result: promotion DOM collection succeeded with 14 cards, stable for 3 polls, contiguous card order, headline `Go all in on big-screen action`; final targets grew from 313 to 315 with promotion-only SKUs `6673119` and `6673143`; detail and availability collection for both SKUs succeeded; recovery then failed before DB load with `promotion final_output new-row SKU mismatch: actual=[] expected=['6673119', '6673143']`
- Root cause: TV `final_output.csv` intentionally has no `sku_id` column, but recovery subset validation matched only that field instead of deriving the SKU from `/sku/<id>` in `product_url` or the final-target lookup. In addition, the review20 step's default empty `BESTBUY_DETAIL_SKUS` overwrote the two-SKU recovery scope and retried two unrelated historical review failures.
- Raw artifacts: RDP console output supplied by the operator; staged recovery artifacts under `promotion_recovery\20260901_013226`; the failed recovery's managed rollback restored canonical CSV/promotion artifacts
- Code changed: `bestbuy/sos_refill.py`, `tests/test_promotion_recovery.py`
- Interpretation and next action: resolve SKU identity from product URL/target lookup for subset validation and preservation, retain `BESTBUY_DETAIL_SKUS` through review20, run the focused and Best Buy test suites, then rerun the same batch-id shortcut. No network/proxy variant test is needed because collection itself succeeded.

### Local verification note

- `python -m pytest tests/test_promotion_recovery.py -q` could not report test results because the repository's pytest capture teardown raised `ValueError: I/O operation on closed file`; this is a local test-runner/capture failure, not a crawler request failure.
- The same suite is therefore run with Python `unittest`, which does not use pytest output capture.
- `python -m unittest -v tests.test_promotion_recovery`: 21 tests passed.
- `python -m unittest -v tests.test_promotion_recovery tests.test_detail_browser_recovery tests.test_interrupt_policy`: 55 tests passed; elapsed 0.030 seconds; no network calls and no crawler runtime artifacts were created.
