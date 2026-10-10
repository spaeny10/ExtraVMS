# Axiom Vision roles

Who can do what, across the hub (hub.axiomvision.ai) and the servers it manages. Everything a person does through the hub is checked against these roles; the audit log records who did it.

## The hierarchy roles apply to

```
Customer  (an organization: "Jetstream Systems")
└── Site  (a place: "Main Facility", with an address, coordinates, time zone, contacts, SOC monitoring)
    └── Server  (one Axiom Vision box at that Site)
        └── Camera
```

A person holds **one role per Customer**, and that role covers every Server and Camera in the Sites they can see. There are three independent kinds of access:

| Kind | Where it is stored | Who it is for |
|---|---|---|
| **Customer role** (viewer, operator, admin, owner) plus a **Site scope** | the Customer's Members list | the customer's own people |
| **Hub administrator** | a flag on the user | Axiom Vision staff who run the hub |
| **SOC role** (operator, supervisor) | a flag on the user | Axiom Vision's monitoring center staff |

A user can hold more than one. The widest applicable access wins for each action.

## Customer roles

Roles nest: each one includes everything below it.

| Role | Watch | Act | Configure servers and Sites | Manage the Customer |
|---|---|---|---|---|
| **Viewer** | yes | | | |
| **Operator** | yes | yes | | |
| **Admin** | yes | yes | yes | yes |
| **Owner** | yes | yes | yes | yes, plus rename the Customer |

**Watch** means everything read-only: Live video, Timeline and playback, Find and search, event details and clips, alerts (viewers may also acknowledge them, on the Sites they can see), Site maps and addresses, dashboards, the fleet overview, and asking the AI (Ask, clip chat). Viewers also reach video directly over the LAN when they are on the same network as a server.

**Act** adds the things an operator does during a shift: PTZ, relays and presets; marking events (feedback such as false alarm or wrong class, editing or regenerating a synopsis, locking an event, watching a person or vehicle, naming identities, reprocessing); saved Find views, layouts and dashboards; briefings on demand; arming or disarming a Site's SOC monitoring for a short period with a reason; running fleet actions that the server's own rules allow an operator to run (on the hub, locking footage from Customer › Actions, where every fleet instruction is planned and confirmed), and undoing their own fleet actions there. Admins see every fleet action in the Customer's Action log and can undo any of them; everyone else sees only their own.

**Configure** adds server and Site setup: cameras, zones and named places, a PTZ camera's settings (such as its return-home timer), retention (including applying a system-optimizer suggestion, which can change it), topology, remote AI, backups and restores, site rules; claiming a new server into the Customer, moving it between Sites, rotating its token, retiring or deleting it; creating, editing and deleting Sites, including address, map pin and time zone; a Site's SOC monitoring schedule, contacts and procedures.

**Manage the Customer** is admin and above with a *real* membership (see the SOC note below): adding and removing members, setting each member's role and Site scope, creating invites, reading the Customer's audit log, and deleting a Site that still has servers.

**Owner** is the only role that can rename the Customer itself. Billing, when it exists, will be owner-only too.

### Site scope

Every member has either **all Sites** of the Customer or a list of **specific Sites**. The scope narrows what the role applies to: an operator granted only "Main Facility" sees that Site's servers, cameras, events and alerts and nothing else of the Customer's. A member with no Sites granted sees nothing until an admin grants some. Admins set this in Customer › Members.

## Hub administrators

A hub administrator is an owner of every Customer, present and future, without being listed as a member anywhere. They see the "All customers" view, manage other hub administrators and SOC staff, and count as a SOC supervisor. Reserve this for Axiom Vision staff.

Granting: Account › Hub administrators (type the email of an existing account), or on the hub server `python -m hub setsuper EMAIL` (`--off` to revoke). The person must have signed up first, normally through an invite to any Customer. The last hub administrator cannot be removed.

Central recording is placed by them alone, on the Hosts page: datacenter hosts and their tokens, allocating an instance on a host to a customer's Site (storage and a camera limit), and changing it there (storage, CPU and memory, camera limit, the Site networks its firewall opens: LAN / VPN subnets and router addresses) or removing it; a central instance's server moves to another Site only by them, and never into a Site that has one. A Site's admins (Customer admins or owners who can see that Site) see the central recording details read-only on the instance's own server card on the Servers tab (progress until it runs, connection, storage, cameras against the limit, the camera addresses in use and the Peplink settings sheet, to set up the site's router; everyone else who sees the Site sees only its "Datacenter (central recording)" tag and storage); its card offers no Retire, Remove, Rotate token or Move (nor does the Actions page retire it, migrate it away or restore it for them), and add the instance's cameras on its console with the admin role, as on any server: each camera's address opens on the instance's firewall by itself, within the rules in tools/central/README.md (a private address inside the Site's networks; a public address that is not another Site's, the hub's or the FusionHub's; no DNS name that spells out an IP; no more cameras than the limit, a backup restore included). Host outages (`host_offline`) and growing work queues on a host (`host_queue_growing`: an instance's YOLO verify queue, or the shared Qwen) notify hub administrators only; the Hosts page shows each host's queues.

Cellular coverage (CoverageMap: carrier scores, FCC signal and coverage, nearby speed tests, and whether the upload carries the Site's cameras) depends on the hub's plan. On the **trial** plan it is evaluation only: hub administrators alone see it or look anything up, marked "Evaluation only", and no customer ever sees it. On the **paid** plan everyone who can see a Site sees its coverage (Settings › General beside the address and map, the 📶 chip in the Site header, the rings on its map); looking a Site up again, or checking an address before a Site exists (Settings › General), costs units and is for hub administrators and the Site's admins (a real customer membership). Data looked up during the trial stays hidden from customers until it has been looked up again on the paid plan. The monthly unit budget and its alert (`coverage_budget`) are for hub administrators only (Account › CoverageMap).

## SOC roles

The Security Operations Center is one internal team. A Customer opts a Site into monitoring (Site › Settings › Monitoring, with an arming schedule, holidays and overrides). SOC staff only ever see Customers that have at least one monitored Site.

| SOC role | In the SOC workstation | In monitored Customers |
|---|---|---|
| **SOC operator** | queue, claim, release, hand off, respond (contacts, procedures, relay, notes), resolve with a disposition, sweep the quiet lane, arm or disarm a Site | acts as an **operator** on every Site |
| **SOC supervisor** | everything an operator can, plus take over and reassign incidents, verify or reject resolutions (four-eyes), set SLA targets, run and read reports | acts as an **admin** on every Site: can configure monitoring schedules, contacts, procedures and servers |

Two limits protect customers from the SOC widening: SOC staff can never manage a Customer's members, invites or audit log unless they also hold a real membership with that role, and the widening disappears the moment a Customer has no monitored Site left. A SOC supervisor cannot verify an incident they resolved themselves.

Granting: there is no screen for this yet. A hub administrator runs `python -m hub setsoc EMAIL operator|supervisor|off` on the hub server (the API `/api/hub/soc/members` exists for a future page). The person must have an account first. SOC staff with no customer membership land on the SOC workstation when they sign in.

## How access combines

| Situation | Result |
|---|---|
| Viewer at Customer A, SOC operator, A has a monitored Site | operator on all of A's Sites (the wider of the two) |
| Admin at Customer A, SOC operator, Customer B monitored | admin at A; operator at B; nothing at C |
| SOC supervisor, Customer B stops monitoring | loses access to B immediately |
| Hub administrator | owner everywhere; supervisor in the SOC |

## Where roles are enforced

The hub checks the role on every request, including video and API calls it tunnels to a server: the server receives the signed user and role and applies the same table, and anything the hub does not recognize as read-only needs admin, so a new server feature is never accidentally open to viewers. Direct-on-LAN playback uses a short-lived token minted by the hub that carries the same role.

Two paths do not go through the hub:

- **The server's own web page on its LAN** (http://server:8080) has no sign-in. Anyone who can reach that address on the local network has full control of that server. Treat the LAN as the boundary, or keep that port off networks you do not trust.
- **The hub command line** on the hub server (`python -m hub …`) is for whoever can SSH to the box.

## Day-to-day: who should get what

| Person | Give them |
|---|---|
| Customer's receptionist or guard who watches cameras | Viewer, scoped to their Site |
| Customer's shift lead who marks events and uses PTZ | Operator |
| Customer's IT or facilities manager | Admin |
| Customer's business owner or main contact | Owner |
| Axiom Vision staff running the hub | Hub administrator |
| Axiom Vision monitoring staff | SOC operator; team leads SOC supervisor |

## Quick reference: how to grant each one

- **Customer role and Site scope**: Customer › Members. Invite new people from Customer › Invites with a role and Site scope baked into the invite.
- **Hub administrator**: Account › Hub administrators, or `setsuper`.
- **SOC role**: `setsoc` on the hub server (no screen yet).
- **Site monitoring, contacts, procedures**: Site › Settings; needs admin at that Customer or SOC supervisor.

Every grant and revocation writes an audit row with who did it.
