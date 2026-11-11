# 13: Extra web hostnames and Vite HMR

**What to build:** A multi-host app can declare `hosts: [admin, portal]` to be reached at `admin.<set>.test` and `portal.<set>.test` on the same container and IP, and a front-end app can declare `vite: 5173` so its dev server and hot reload work through the set (stories 64, 65, 69).

**Blocked by:** 12 (HTTPS web apps)

**Status:** done

- [x] Extra hosts route to the web service's container and IP, are added as network aliases and to the hosts block, and are covered by the set's wildcard certificate
- [x] Extra host names more than one label deep fail validation
- [x] Extra hosts participate in the duplicate-hostname check
- [x] `vite: 5173` publishes that port on the app's IP and sets the HMR host to the app's hostname
- [x] Fixture web repo declares an extra host; reachable over HTTPS from host and agent
