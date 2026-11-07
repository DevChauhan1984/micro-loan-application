MICROLOAN MANAGER V1.5 - PRODUCTION DEPLOYMENT
===============================================

Recommended architecture
-------------------------
Run ONE central server installation on a reliable Windows desktop/server at the
main office or branch/office PC. All branch desktops/laptops use a browser to
access the same central application.

This is important: installing separate local databases on every branch PC would
create separate datasets and the "All Branches" dashboard would NOT consolidate
live data across machines.

Server setup
------------
1. Install Python 3 on the central Windows machine.
2. Copy/extract this complete folder to a permanent location, e.g.
   C:\MicroLoanManager\
3. Run run_server.bat
4. The server listens on port 5050.
5. On the server PC, open:
   http://127.0.0.1:5050/
6. Find the server PC's LAN IPv4 address with:
   ipconfig
   Example: 192.168.1.50
7. From a branch PC on the same LAN, open:
   http://192.168.1.50:5050/
8. If Windows Firewall prompts, allow Python on the PRIVATE network only.

Branch desktops/laptops
----------------------
No application installation is required on branch PCs. They only need a modern
browser and network access to the central server.

Create a browser shortcut for:
http://<SERVER-IP>:5050/

For example:
http://192.168.1.50:5050/

Data and backups
----------------
The production database is:
data\microloan.db

Automatic startup backups are created under:
backups\microloan_startup_YYYYMMDD_HHMMSS.db

The application also has the Backup page for on-demand backups.
Default startup backup retention is 30 days and can be changed in config.json.

Production data policy
----------------------
- Do not delete or replace data\microloan.db once live data is loaded.
- Back up the entire MicroLoanManager folder regularly to a separate disk/cloud
  location.
- Keep the server PC powered on during operating hours.
- Use Windows login/password and disk protection on the server PC.
- Restrict the Windows firewall to the PRIVATE network and required users.

Important security note
-----------------------
V1.5 is designed as a controlled internal LAN deployment. It is NOT intended
to be exposed directly to the public internet. Do not port-forward port 5050
to the internet.

Before live use
---------------
1. Test branch creation.
2. Test customer -> loan -> collection branch inheritance.
3. Test All Branches dashboard.
4. Load a small legacy sample and reconcile totals.
5. Take a backup and verify the backup file.
6. Then load the complete legacy data.

Current edition
---------------
MicroLoan Manager V1.5 Production Edition
No sample/test transactions are seeded automatically.
