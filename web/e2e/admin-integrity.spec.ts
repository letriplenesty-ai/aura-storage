import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

/**
 * Integrity scrub report on the admin overview. Uses the local API (see
 * admin-tabs.spec.ts); the second test replaces only the integrity response to
 * show a damaged file. S3 is never contacted.
 */

async function loginToAdmin(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Identifiant").fill("admin");
  await page.getByLabel("Mot de passe").fill("local-smoke-test-password");
  await page.getByRole("button", { name: "Se connecter", exact: true }).click();
  await page.getByRole("link", { name: "Administration" }).click();
  await expect(page).toHaveURL(/\/admin/);
}

test("the overview states how often files are re-checked", async ({ page }) => {
  await loginToAdmin(page);
  await expect(page.getByRole("heading", { name: "Intégrité des fichiers" })).toBeVisible();
  await expect(page.getByTestId("integrity-summary")).toContainText("au moins tous les 30 jours");
});

test("damaged and lost files are listed with a restore warning", async ({ page }) => {
  await page.route("**/api/v1/admin/integrity", (route) =>
    route.fulfill({
      json: {
        interval_days: 30,
        results: {
          ok: { count: 41, oldest: 1_700_000_000 },
          mismatch: { count: 1, oldest: 1_700_000_100 },
          missing: { count: 1, oldest: 1_700_000_200 },
        },
        problems: [
          { id: "aaaaaaaa-1", space_id: "s-1", display_name: "contrat.pdf", size_bytes: 10, scrub_result: "mismatch", scrubbed_at: 1_700_000_100 },
          { id: "bbbbbbbb-2", space_id: "s-1", display_name: "audio.mp3", size_bytes: 20, scrub_result: "missing", scrubbed_at: 1_700_000_200 },
        ],
      },
    }),
  );
  await loginToAdmin(page);
  await expect(page.getByTestId("integrity-summary")).toContainText("41 intact(s)");
  await expect(page.getByTestId("integrity-summary")).toContainText("2 en défaut");
  await expect(page.getByText("restaurez-les depuis la sauvegarde")).toBeVisible();
  await expect(page.getByRole("cell", { name: "Contenu altéré" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "Absent du stockage" })).toBeVisible();
});
