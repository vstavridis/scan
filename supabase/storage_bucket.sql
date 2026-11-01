-- Δημιουργία PRIVATE bucket για τα αρχεία Excel (εναλλακτικά: Storage -> New bucket, Public = OFF).
-- Εκτελέστε στο Supabase -> SQL Editor.
insert into storage.buckets (id, name, public, file_size_limit)
values ('warehouse-excel', 'warehouse-excel', false, 52428800)
on conflict (id) do update set public = false;
-- Δεν χρειάζονται policies: η εφαρμογή χρησιμοποιεί το service key (server-side) και
-- κανένας άλλος (anon/authenticated) δεν έχει πρόσβαση στο private bucket.
