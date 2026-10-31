# Concepts

Shared domain vocabulary for this project — entities, named processes, and status
concepts with project-specific meaning. Seeded with core domain vocabulary, then
accretes as ce-compound and ce-compound-refresh process learnings; direct edits are
fine. Glossary only, not a spec or catch-all.

## Relationships

An Item belongs to at most one Category and is kept at most one Location. Categories
nest into one another without limit; Locations never nest. Categories are owned — one
Category has one parent — while Locations are shared: many Items in unrelated
Categories can name the same Location. Neither a Category holding anything nor a
Location an Item still names can be deleted.

## Inventory

### Item
One kind of thing the owner possesses, recorded with a count rather than one row per
physical object.

Every Item carries the same three fields whatever it is — a name, a quantity, and
freeform notes. This uniformity is deliberate: a book's author and a record's artist
live in the notes rather than in fields of their own, which is why search reads notes
as well as names, and why nothing can be sorted by an attribute that only some kinds
of thing possess. An Item may name no Category at all, in which case it is reachable
only by search or by the view that collects such Items, since every browsing path
descends through Categories.

### Category
A named group of Items that may also contain other Categories, nested to any depth.

A Category holds sub-categories and Items at the same time — Items attach at whatever
level the owner finds natural, not only at the bottom of the tree. Names are unique
among siblings rather than across the whole tree, so the same name may appear under
two different parents. A Category cannot be deleted while it still holds
sub-categories or Items.

### Collection
A Category with no parent — one of the top-level groupings the owner sees first.

Collection is a position in the tree, not a separate kind of thing: every Collection
is a Category, and a Category becomes one precisely by having no parent. The
distinction matters because an Item attached directly to a Collection sits at the top
level, which is different from an Item attached to no Category at all.

### Location
A flat label naming where an Item is kept.

Locations deliberately do not nest, unlike Categories — they are a single vocabulary
of place names shared across the whole inventory, so unrelated Items may name the same
Location. A Location is created on first use by naming it, reused by name afterwards,
and cannot be deleted while any Item still names it.

## Access

### Owner
The one person whose inventory PRIMS holds, and its only user.

There are no accounts: one password belongs to the Owner, and signing in on any
device means proving to be the Owner. Everything the Owner can do from the machine
running PRIMS, a signed-in phone can do as well.

### Loopback exemption
The rule that a request made from the machine running PRIMS needs no sign-in.

The exemption is judged only by the address the connection actually comes from,
never by anything a request says about itself, since such a claim costs nothing to
forge. Because every other device must sign in, PRIMS refuses to serve beyond the
machine until a password has been set; serving to the machine alone needs none, so
a fresh setup always runs.

### Sign-out everywhere
Revoking every signed-in device at once — the remedy for a lost phone.

Each sign-in is stamped with the current session generation, and sign-out everywhere
advances it, so every earlier sign-in stops counting on its next request. Changing
the password deliberately does not do this: other devices stay signed in, which is
why a password change is not the answer to a lost phone.
