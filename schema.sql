-- ============================================================
-- WhatsApp Review Engine — Supabase SQL Schema
-- ============================================================

-- Enable pgcrypto for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- -----------------------------------------------------------
-- 1. businesses — one row per subscribed tradesperson / company
-- -----------------------------------------------------------
CREATE TABLE businesses (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_name            TEXT        NOT NULL,
    business_name         TEXT        NOT NULL,
    phone_number          TEXT        NOT NULL UNIQUE,   -- owner's WhatsApp number in E.164
    trade_type            TEXT,                          -- e.g., 'Electrician', 'Plumber', 'Landscaper'
    google_place_id       TEXT,                          -- Google Business Profile place ID
    google_review_link    TEXT,                          -- direct Google review URL
    google_refresh_token  TEXT,                          -- Fernet-encrypted OAuth refresh token
    google_account_id     TEXT,                          -- GBP account ID
    google_location_id    TEXT,                          -- GBP location ID
    stripe_customer_id    TEXT,                          -- Stripe Customer ID
    stripe_subscription_id TEXT,                         -- Stripe Subscription ID
    subscription_status   TEXT        NOT NULL DEFAULT 'active'
                              CHECK (subscription_status IN ('active', 'past_due', 'inactive', 'trial', 'demo')),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_businesses_phone ON businesses (phone_number);

-- -----------------------------------------------------------
-- 2. customers — every end-customer who receives a review request
-- -----------------------------------------------------------
CREATE TABLE customers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     UUID        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name            TEXT        NOT NULL,
    phone_number    TEXT        NOT NULL,           -- customer WhatsApp number in E.164
    review_requested_at TIMESTAMPTZ,
    review_link_sent    BOOLEAN NOT NULL DEFAULT FALSE,
    status          TEXT        NOT NULL DEFAULT 'request_sent'
                        CHECK (status IN ('request_sent', 'clicked_great', 'clicked_bad', 'review_posted')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_customers_business ON customers (business_id);
CREATE INDEX idx_customers_phone    ON customers (phone_number);

-- Prevent duplicate review requests for same customer+business pair
CREATE UNIQUE INDEX idx_customers_business_phone ON customers (business_id, phone_number);

-- -----------------------------------------------------------
-- 3. review_drafts — AI-generated replies awaiting owner approval
-- -----------------------------------------------------------
CREATE TABLE review_drafts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     UUID        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    google_review_id TEXT       NOT NULL,           -- review ID from Google API
    reviewer_name   TEXT,
    review_text     TEXT,
    star_rating     SMALLINT,
    ai_draft_reply  TEXT        NOT NULL,           -- AI-generated reply
    status          TEXT        NOT NULL DEFAULT 'pending_approval'
                        CHECK (status IN ('pending_approval', 'approved', 'awaiting_edit', 'edited', 'rejected', 'posted')),
    sent_to_owner   BOOLEAN    NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_review_drafts_business ON review_drafts (business_id);
CREATE INDEX idx_review_drafts_status   ON review_drafts (status);

-- -----------------------------------------------------------
-- Trigger: auto-update `updated_at` on row change
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_businesses_updated
    BEFORE UPDATE ON businesses
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_review_drafts_updated
    BEFORE UPDATE ON review_drafts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- -----------------------------------------------------------
-- Row Level Security (enable, then add policies as needed)
-- -----------------------------------------------------------
ALTER TABLE businesses    ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers     ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_drafts ENABLE ROW LEVEL SECURITY;

-- Service-role bypass (used by your backend via SUPABASE_KEY)
CREATE POLICY service_all ON businesses    FOR ALL USING (TRUE) WITH CHECK (TRUE);
CREATE POLICY service_all ON customers     FOR ALL USING (TRUE) WITH CHECK (TRUE);
CREATE POLICY service_all ON review_drafts FOR ALL USING (TRUE) WITH CHECK (TRUE);
