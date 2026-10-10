-- Card identities and explicit local targets only. PAN and bank data stay out of SQLite.
CREATE TABLE server_cards (
    card_id TEXT PRIMARY KEY CHECK (
        length(card_id)=32 AND card_id NOT GLOB '*[^0-9a-f]*'),
    connection_id TEXT NOT NULL REFERENCES app_bank_connections(id),
    owner_id TEXT NOT NULL REFERENCES app_users(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    masked_number TEXT NOT NULL CHECK (
        length(masked_number)=9 AND substr(masked_number,1,5)='•••• '
        AND substr(masked_number,6) NOT GLOB '*[^0-9]*'),
    UNIQUE (connection_id, account_id)
);

CREATE INDEX server_cards_owner_connection ON server_cards(owner_id, connection_id);
