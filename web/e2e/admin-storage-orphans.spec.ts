import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

/**
 * Storage orphans card: scanned only on demand. Uses the local API for the
 * page; the scan response itself is replaced so no storage is needed.
 */

async function loginToAdmin(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Identifiant").fill("admin");
  await page.getByLabel("Mot de passe").fill("local-smoke-test-password");
  await page.getByRole("button", { name: "Se connecter", exact: true }).click();
  await page.getByRole("link", { name: "Administration" }).click();
  await expect(page).toHaveURL(/\/admin/);
}

test("the storage is only scanned on demand and the result is summarised", async ({ page }) => {
  let scans = 0;
  await page.route("**/api/v1/admin/storage-orphans", (route) => {
    scans += 1;
    return route.fulfill({
      json: {
        grace_seconds: 86400,
        checked_at: 1_700_000_000,
        buckets: [
          {
            bucket: "aura", scanned: 120, orphan_bytes: 3 * 1024 * 1024,
            orphans: [
              { key: "aura/s/a/1", size_bytes: 1024 * 1024, last_modified: 1, reason: "no_row", object_id: null },
              { key: "aura/s/a/2", size_bytes: 2 * 1024 * 1024, last_modified: 1, reason: "terminal", object_id: "o" },
            ],
            stale_uploads: [{ key: "aura/s/a/3", upload_id: "u", initiated: 1 }],
            next_start_after: null,
          },
          { bucket: "archives", error: "storage unavailable" },
        ],
      },
    });
  });
  await loginToAdmin(page);
  await expect(page.getByRole("heading", { name: "Octets non référencés" })).toBeVisible();
  expect(scans).toBe(0);

  await page.getByRole("button", { name: "Analyser le stockage" }).click();
  const list = page.getByTestId("storage-orphans");
  await expect(list).toContainText("aura : 2 fichier(s) non référencé(s) (3.0 Mo)");
  await expect(list).toContainText("1 envoi(s) en parties abandonné(s) sur 120");
  await expect(list).toContainText("archives : stockage indisponible");
  expect(scans).toBe(1);
});
