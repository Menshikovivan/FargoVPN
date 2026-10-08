-- Safe for existing payments: historical purchase tokens remain NULL.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS purchase_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_purchase_token
    ON payments(purchase_token) WHERE purchase_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payments_user_status ON payments(tg_id,status);
