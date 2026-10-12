const { test, expect } = require("@playwright/test");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const { spawn } = require("node:child_process");

test("upload, search, inspect checksum, and download identical bytes", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const content = Buffer.from("A small file, a real TCP transfer.\n\x00\xff");
  const name = "browser transfer=verified.txt";
  await page.goto("/");
  await expect(page.locator("#header-status")).toContainText("Connected");
  await page.locator("#file-input").setInputFiles({
    name,
    mimeType: "application/octet-stream",
    buffer: content,
  });
  await expect(
    page.getByRole("button", { name: `Details for ${name}`, exact: true }),
  ).toBeVisible();
  await expect(page.locator("#recent-transfers")).toContainText("Verified");
  await page.getByRole("searchbox").fill("does not exist");
  await expect(page.locator("#empty-title")).toHaveText(
    "Nothing by that name.",
  );
  await page.getByRole("button", { name: "Clear search", exact: true }).click();
  await page
    .getByRole("button", { name: `Details for ${name}`, exact: true })
    .click();
  await expect(page.locator("#detail-checksum")).toHaveText(
    crypto.createHash("sha256").update(content).digest("hex"),
  );
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#detail-download").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(name);
  expect(await fs.readFile(await download.path())).toEqual(content);
  await page.locator("#nav-activity").click();
  await expect(page.locator("#activity-list")).toContainText("Uploaded");
  await expect(page.locator("#activity-list")).toContainText("Downloaded");
  expect(errors).toEqual([]);
  await page.screenshot({
    path: "test-results/activity-desktop.png",
    fullPage: true,
  });
});

test("empty files and multiple-file upload work", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#header-status")).toContainText("Connected");
  await page.locator("#file-input").setInputFiles([
    {
      name: "empty-browser.txt",
      mimeType: "text/plain",
      buffer: Buffer.alloc(0),
    },
    {
      name: "second-browser.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("second file"),
    },
  ]);
  await expect(
    page.getByRole("button", {
      name: "Download empty-browser.txt",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "Download second-browser.txt",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.locator("#upload-button")).toBeEnabled();
  const downloadPromise = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download empty-browser.txt", exact: true })
    .click();
  const download = await downloadPromise;
  expect((await fs.readFile(await download.path())).length).toBe(0);
  await page.screenshot({
    path: "test-results/files-desktop.png",
    fullPage: true,
  });
});

test("mobile layout, disconnected state, connection error, and reconnect", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.locator("#header-status")).toContainText("Connected");
  await page.locator("#file-input").setInputFiles({
    name: "mobile-browser.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("mobile file"),
  });
  await expect(
    page.getByRole("button", {
      name: "Details for mobile-browser.txt",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.locator("#upload-button")).toBeEnabled();
  await page.locator("#connection-button").click();
  await page.getByRole("button", { name: "Disconnect", exact: true }).click();
  await expect(page.locator("#header-status")).toHaveText("Connect a server");
  await page.getByRole("button", { name: "Upload files", exact: true }).click();
  await expect(page.locator("#connection-dialog")).toBeVisible();
  await page.locator("#port-input").fill("65534");
  await page.locator("#connect-submit").click();
  await expect(page.locator("#connection-error")).toContainText("unavailable");
  await page.locator("#port-input").fill("9001");
  await page.locator("#connect-submit").click();
  await expect(page.locator("#connection-dialog")).not.toBeVisible();
  await expect(page.locator("#header-status")).toContainText("Connected");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/files-mobile.png",
    fullPage: true,
  });
  await page
    .getByRole("button", {
      name: "Details for mobile-browser.txt",
      exact: true,
    })
    .click();
  await expect(page.locator("#detail-dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("#detail-dialog")).not.toBeVisible();
});

test("drag-and-drop uploads a real file", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#header-status")).toContainText("Connected");
  const transfer = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.items.add(
      new File(["Dropped into the workspace."], "dropped-browser.txt", {
        type: "text/plain",
      }),
    );
    return data;
  });
  await page
    .locator("#drop-zone")
    .dispatchEvent("drop", { dataTransfer: transfer });
  await expect(
    page.getByRole("button", {
      name: "Details for dropped-browser.txt",
      exact: true,
    }),
  ).toBeVisible();
});

test("broadcast reaches the workspace and a separate connected client", async ({
  page,
}) => {
  const receiverDirectory = path.resolve(
    ".web-run",
    `ui-receiver-${Date.now()}`,
  );
  const receiver = spawn(
    "python",
    [
      "-m",
      "cftp.client",
      "--port",
      "9001",
      "--download-dir",
      receiverDirectory,
      "listen",
    ],
    { windowsHide: true },
  );
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error("Listener did not register")),
        10000,
      );
      receiver.stdout.on("data", (chunk) => {
        if (chunk.toString().includes("Listening as")) {
          clearTimeout(timer);
          resolve();
        }
      });
      receiver.on("error", (error) => {
        clearTimeout(timer);
        reject(error);
      });
      receiver.on("exit", (code) => {
        if (code) {
          clearTimeout(timer);
          reject(new Error(`Listener exited: ${code}`));
        }
      });
    });
    const name = "broadcast-browser.txt";
    const content = Buffer.from("Broadcast to every connected CFTP client.");
    await page.goto("/");
    await expect(page.locator("#header-status")).toContainText("Connected");
    await page
      .locator("#file-input")
      .setInputFiles({ name, mimeType: "text/plain", buffer: content });
    await expect(
      page.getByRole("button", { name: `Broadcast ${name}`, exact: true }),
    ).toBeEnabled();
    await page
      .getByRole("button", { name: `Broadcast ${name}`, exact: true })
      .click();
    await expect(page.locator("#broadcast-target-count")).toContainText(
      "All 2 connected clients",
    );
    await page.locator("#broadcast-submit").click();
    await expect(page.locator("#broadcast-jobs")).toContainText(
      "2/2 verified",
      { timeout: 15000 },
    );
    await expect(page.locator("#inbox-list")).toContainText(name);
    await expect
      .poll(async () => {
        try {
          return (
            await fs.readFile(path.join(receiverDirectory, name))
          ).toString("hex");
        } catch {
          return "";
        }
      })
      .toBe(content.toString("hex"));
    const downloadPromise = page.waitForEvent("download");
    await page
      .getByRole("button", { name: `Save received ${name}`, exact: true })
      .click();
    const download = await downloadPromise;
    expect(await fs.readFile(await download.path())).toEqual(content);
    await page.locator("#broadcast-jobs summary").first().click();
    await page.waitForResponse(
      (response) =>
        response.url().includes("/api/broadcasts") && response.status() === 200,
    );
    await expect(
      page.locator("#broadcast-jobs details").first(),
    ).toHaveAttribute("open", "");
    await page.screenshot({
      path: "test-results/broadcasts-desktop.png",
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: "test-results/broadcasts-mobile.png",
      fullPage: true,
    });
  } finally {
    receiver.kill();
  }
});
