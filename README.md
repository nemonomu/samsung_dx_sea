# samsung_dx_sea

Walmart raw HTTP collection pipeline for SEA retail collection.

## Layout

- `walmart/common`: shared Walmart raw HTTP, parsing, normalization, validation, and DB helpers
- `walmart/tv`: TV-specific runner entrypoint
- `walmart/hhp`: placeholder for the HHP-specific runner entrypoint

## Setup

1. Copy `config.example.py` to `config.py`.
2. Fill `DB_CONFIG` in `config.py`.
3. Run `walmart\tv\run_walmart_tv_pipeline.bat` on the RDP machine.

The pipeline runs:

1. listing collection
2. detail/review collection
3. missing SKU recovery
4. chunk merge
5. DB shape normalization
6. validation 3 times
7. `tv_retail_com` insert and `tv_item_mst` upsert

DB insert is blocked automatically if validation fails or if listing does not meet:

- main accepted SKU count >= 300
- bsr accepted SKU count >= 100
