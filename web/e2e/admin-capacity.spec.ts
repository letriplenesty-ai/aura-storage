import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

/** Capacity line in the admin usage card (local API for the page). */

async function loginToAdmin(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Identifiant").fill("admin");
  await page.getByLabel("Mot de passe").fill("local-smoke-test-password");
  await page.getByRole("button", { name: "Se connecter", exact: true }).click();
  await page.getByRole("link", { name: "Administration" }).click();
  await expect(page).toHaveURL(/\/admin/);
}

test("without a configured capacity the page says the disk is not watched", async ({ page }) => {
  await loginToAdmin(page);
  await expect(page.getByTestId("capacity")).toContainText("Aucune capacité configurée");
});

test("a nearly full store is shown as an alert", async ({ page }) => {
  await page.route("**/api/v1/admin/usage", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.capacity = {
      capacity_bytes: 1000 * 1024 ** 3, high_watermark_pct: 90,
      accept_until_bytes: 900 * 1024 ** 3, stored_bytes: 880 * 1024 ** 3,
      reserved_bytes: 10 * 1024 ** 3, free_for_uploads_bytes: 10 * 1024 ** 3,
    };
    await route.fulfill({ response, json: body });
  });
  await loginToAdmin(page);
  const line = page.getByTestId("capacity");
  await expect(line).toHaveClass(/alert-error/);
  await expect(line).toContainText("au-delà de 90 %");
});
