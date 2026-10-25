# Satellite catalog

Public CelesTrak Current Data (GP) snapshots for the ISSTracker Apple and Android apps. No favorites, account data, or device selections are uploaded here. Supplemental GP data is outside this feed.

## Publication

GitHub Actions runs every six hours at 00:23, 06:23, 12:23, and 18:23 UTC, or on manual dispatch. GitHub Pages publishes the generated JSON without committing orbital datasets into source history. Scheduling is best effort, not a freshness guarantee. Public scheduled workflows can be disabled by GitHub after 60 days without repository activity; maintainers must monitor the Actions page and re-enable them when necessary.

Canonical base URL: `https://neonindigo.com/satellite-catalog/` (the organization's existing GitHub Pages custom domain).

- `v1/manifest.json`: discovery timestamp, publication timestamp, completeness, per-group freshness and errors, content-addressed group/index paths, and upstream halt status.
- `v1/iss.json`: small bootstrap record for ISS tracking before the catalog finishes downloading.
- `v1/blobs/<sha256>.json`: immutable group snapshots and deduplicated search index. Group snapshots contain full OMM JSON rows. Index objects identify their group memberships and orbital-data endpoint.
- `v1/objects/<NORAD>.json`: current orbital elements, last successful upstream fetch time, and group memberships for a single object.

Clients fetch the manifest first, then its referenced index/groups. Group files become browseable independently; search must remain labeled incomplete until all required data has loaded and upstream coverage is complete. If a referenced blob is no longer available after a later deployment, refetch the manifest. The publisher retains unreferenced blobs for 48 hours to bound publication size, and always retains all files referenced by its current manifest.

All timestamps use UTC. `epoch` comes from the orbital elements, `fetchedAt` is the successful upstream download time, and `publishedAt` is the feed build time. **Publication or download never resets orbital age.** The app's 24-hour warning and 72-hour cutoff use the epoch. App Refresh downloads this feed and does not trigger an upstream workflow. Cache successful app downloads for offline use and retain them if GitHub fails.

Objects are identified by NORAD catalog number (up to nine digits); JSON orbital elements avoid TLE's five-digit limit. Names are labels, not identity. Search results deduplicate by catalog number. An object's absence in one group does not establish its absence from the catalog. Groups that fail refresh retain their previous entries and expose stale status.

## Failure handling

The publisher restores local state from Actions cache, falling back to the published Pages feed if cache restoration fails. It never treats missing previous state as an empty successful catalog. Discovery failure retains known groups; an unrecognized/empty discovery response cannot remove groups. On initial failure it refuses to publish an empty catalog.

Requests run sequentially with a pause and without automatic HTTP redirects. In accordance with [CelesTrak's usage policy](https://celestrak.org/usage-policy.php), any non-200 response stops further upstream requests and stores a halt marker. Later scheduled runs do not query upstream while halted. Inspect the workflow logs, correct the cause, and dispatch **Refresh catalog** with `resume_upstream=true` to explicitly resume. Publication still preserves last successful data and marks incomplete coverage. Failures are reported in the workflow summary and run status after publication.

The schedule is every six hours. A two-hour minimum between successful upstream downloads also applies to manual dispatches and code-change runs, matching CelesTrak's update interval. This allows normal scheduler delays without accidentally skipping the next six-hour run. There are no automatic retries within a run. Failed non-HTTP requests may be attempted on a later run. The previous Pages deployment remains live if publication itself fails.

## Development

Requires Python 3.12+ and no third-party packages.

```sh
python3 -m unittest discover -s tests -v
python3 publish.py --state state --output public
```

The first real fetch requires internet access to CelesTrak. Generated `state/` and `public/` are ignored. `--resume-upstream` is for an operator who has investigated an upstream HTTP failure.

## Setup

Enable GitHub Pages with **GitHub Actions** as the build source. The workflow needs `pages:write` and `id-token:write` to deploy. Restrict the `github-pages` environment to the default branch. Run the workflow manually, then verify the public manifest and ISS record before configuring the apps to use the feed.

Data source: [CelesTrak Current Data](https://celestrak.org/NORAD/elements/) and [GP format documentation](https://celestrak.org/NORAD/documentation/gp-data-formats.php). This is an independent cache, not an official CelesTrak service.
