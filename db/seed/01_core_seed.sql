-- Datos semilla SOLO para entornos locales (no son una migración).
-- Crea la cuenta de compensación CLEARING, 30 clientes sintéticos y una cuenta por cliente.
-- Los saldos iniciales entran como transferencias INTERNAL COMPLETED desde CLEARING, con su
-- par de asientos D/C en core.transactions. El saldo solo se mueve junto a esos asientos:
-- ninguna cuenta se crea con saldo propio. CLEARING queda en negativo por la suma emitida.
--
-- Idempotente: los identificadores son deterministas y, si la transferencia de un cliente
-- ya existe, ese cliente no se vuelve a acreditar. Ejecutar como verabank_transfer.

BEGIN;

INSERT INTO core.accounts (account_id, customer_id, account_type, currency, status)
VALUES (md5('verabank-seed:clearing')::uuid, NULL, 'CLEARING', 'PEN', 'ACTIVE')
ON CONFLICT (account_id) DO NOTHING;

-- 30 clientes: 18 RETAIL, 8 PREMIUM y 4 PYME.
INSERT INTO core.customers (customer_id, segment, country)
SELECT md5('verabank-seed:customer:' || i)::uuid,
       CASE WHEN i <= 18 THEN 'RETAIL' WHEN i <= 26 THEN 'PREMIUM' ELSE 'PYME' END,
       'PE'
FROM generate_series(1, 30) AS g(i)
ON CONFLICT (customer_id) DO NOTHING;

-- Una cuenta por cliente, con saldo 0 por defecto.
INSERT INTO core.accounts (account_id, customer_id, account_type, currency, status)
SELECT md5('verabank-seed:account:' || i)::uuid,
       md5('verabank-seed:customer:' || i)::uuid,
       'CUSTOMER', 'PEN', 'ACTIVE'
FROM generate_series(1, 30) AS g(i)
ON CONFLICT (account_id) DO NOTHING;

-- Saldos iniciales: una transferencia INTERNAL COMPLETED por cliente desde CLEARING.
WITH plan AS (
  SELECT i,
         md5('verabank-seed:transfer:' || i)::uuid AS transfer_id,
         md5('verabank-seed:idem:' || i)::uuid     AS idempotency_key,
         md5('verabank-seed:account:' || i)::uuid  AS account_id,
         (CASE
            WHEN i <= 18 THEN  500 + (i * 811  % 4501)    -- RETAIL:   500 a  5.000
            WHEN i <= 26 THEN 20000 + (i * 7919 % 80001)  -- PREMIUM: 20.000 a 100.000
            ELSE              10000 + (i * 6007 % 70001)  -- PYME:    10.000 a  80.000
          END + (i * 37 % 100) / 100.0)::numeric(18,2) AS amount
  FROM generate_series(1, 30) AS g(i)
),
clearing AS (
  SELECT account_id, balance
  FROM core.accounts
  WHERE account_id = md5('verabank-seed:clearing')::uuid
),
todo AS (                                   -- clientes aún sin su transferencia semilla
  SELECT p.*
  FROM plan p
  WHERE NOT EXISTS (SELECT 1 FROM core.transfers t
                    WHERE t.client_id = 'seed' AND t.idempotency_key = p.idempotency_key)
),
ins_transfers AS (
  INSERT INTO core.transfers (transfer_id, client_id, idempotency_key, request_hash,
                              source_account_id, dest_account_id, dest_country, rail,
                              transfer_type, amount, currency, status, occurred_at, completed_at)
  SELECT t.transfer_id, 'seed', t.idempotency_key,
         encode(sha256(convert_to('seed|' || t.i || '|' || t.amount::text, 'UTF8')), 'hex'),
         c.account_id, t.account_id, 'PE', 'INTERNAL',
         'TRANSFER', t.amount, 'PEN', 'COMPLETED',
         timestamptz '2026-01-01 00:00:00+00', timestamptz '2026-01-01 00:00:00+00'
  FROM todo t CROSS JOIN clearing c
  RETURNING transfer_id
),
ins_entries AS (                            -- par D/C por transferencia
  INSERT INTO core.transactions (transfer_id, account_id, direction, entry_kind, amount, balance_after)
  SELECT t.transfer_id, c.account_id, 'D', 'TRANSFER', t.amount,
         c.balance - sum(t.amount) OVER (ORDER BY t.i)
  FROM todo t CROSS JOIN clearing c
  UNION ALL
  SELECT t.transfer_id, t.account_id, 'C', 'TRANSFER', t.amount, a.balance + t.amount
  FROM todo t JOIN core.accounts a ON a.account_id = t.account_id
  RETURNING entry_id
),
upd_clearing AS (
  UPDATE core.accounts a
     SET balance = a.balance - s.total, version = a.version + 1
    FROM (SELECT sum(amount) AS total FROM todo) s
   WHERE a.account_id = md5('verabank-seed:clearing')::uuid AND s.total IS NOT NULL
  RETURNING a.account_id
),
upd_customers AS (
  UPDATE core.accounts a
     SET balance = a.balance + t.amount, version = a.version + 1
    FROM todo t
   WHERE a.account_id = t.account_id
  RETURNING a.account_id
)
SELECT (SELECT count(*) FROM ins_transfers) AS transferencias_creadas,
       (SELECT count(*) FROM ins_entries)   AS asientos_creados,
       (SELECT count(*) FROM upd_customers) AS cuentas_acreditadas,
       (SELECT count(*) FROM upd_clearing)  AS clearing_actualizada;

COMMIT;
