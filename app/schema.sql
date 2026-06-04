CREATE TABLE IF NOT EXISTS keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  public TEXT NOT NULL,
  private TEXT NOT NULL,
  symbol TEXT NOT NULL,
  type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  `name` TEXT NOT NULL,
  `value` TEXT,
  UNIQUE(`name`)
);

CREATE TABLE IF NOT EXISTS payout_auth_nonces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  consumer TEXT NOT NULL,
  key_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  timestamp INTEGER NOT NULL,
  created_at TEXT,
  UNIQUE(consumer, key_id, nonce)
);

CREATE INDEX IF NOT EXISTS ix_payout_auth_nonces_created_at
ON payout_auth_nonces(created_at);

CREATE TABLE IF NOT EXISTS payout_executions (
  execution_id TEXT PRIMARY KEY,
  consumer TEXT NOT NULL,
  external_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  sidecar_payload_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  state_version INTEGER NOT NULL,
  state_transition_id TEXT NOT NULL,
  state_updated_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_expires_at TEXT,
  attempt_id TEXT,
  source_wallet TEXT NOT NULL,
  token_contract TEXT NOT NULL,
  resource_reservation_id TEXT,
  reference_block TEXT,
  chain_id_or_network_id TEXT NOT NULL,
  expiration_at TEXT,
  canonical_payload_json TEXT NOT NULL,
  signed_raw_tx_ref TEXT,
  signed_raw_tx_hash TEXT,
  signed_raw_tx_stored_at TEXT,
  txid TEXT,
  broadcast_provider TEXT,
  broadcast_attempted_at TEXT,
  chain_check_metadata TEXT,
  payout_queue TEXT NOT NULL,
  failure_class TEXT,
  error_code TEXT,
  error_message TEXT,
  reconciliation_required INTEGER NOT NULL DEFAULT 0,
  txids_json TEXT,
  message_hashes_json TEXT,
  UNIQUE(consumer, external_id)
);

CREATE INDEX IF NOT EXISTS ix_payout_executions_state_updated
ON payout_executions(state, state_updated_at);

CREATE INDEX IF NOT EXISTS ix_payout_executions_reconciliation
ON payout_executions(reconciliation_required, state_updated_at);

CREATE TABLE IF NOT EXISTS payout_callback_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  claimed_at TEXT,
  claim_token TEXT,
  last_http_status INTEGER,
  last_error TEXT,
  last_response_text TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  sent_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_payout_callback_outbox_status_updated
ON payout_callback_outbox(status, updated_at);

CREATE INDEX IF NOT EXISTS ix_payout_callback_outbox_dispatch_due
ON payout_callback_outbox(status, next_attempt_at, updated_at);
