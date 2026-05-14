import { expect, test } from "@playwright/test";

async function login(page) {
  await page.goto("/");
  await expect(page).toHaveURL(/\/auth\/login/);
  await page.getByLabel("访问密码").fill("hhs54666");
  await page.getByRole("button", { name: "进入项目" }).click();
  await expect(page.getByRole("button", { name: "工作台", exact: true })).toBeVisible();
}

test("登录后可访问四模式工作台和主视图", async ({ page }) => {
  await login(page);

  for (const name of ["对话", "分镜", "生成", "编辑"]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expect(page.locator("form")).toBeVisible();
  }

  for (const name of ["历史", "图库", "提示词", "设置", "工作台"]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expect(page.getByRole("button", { name, exact: true })).toHaveClass(/active/);
  }
});

test("设置页可管理 provider、风格锁和角色档案", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "设置", exact: true }).click();

  await page.locator(".settingsGroupHead").filter({ hasText: "提供商管理" }).click();
  const providerName = `E2E Provider ${Date.now()}`;
  await page.getByPlaceholder("例如 asxs / OpenAI / 备用线路").fill(providerName);
  await page.getByPlaceholder("https://api.example.com/v1").fill("https://e2e.example/v1");
  await page.getByPlaceholder("sk-...").fill("sk-e2e");
  await page.getByRole("button", { name: "新建提供商" }).click();
  await expect(page.locator(".providerItem strong").filter({ hasText: providerName })).toBeVisible();
  await page.getByRole("button", { name: "保存提供商选择" }).click();
  await expect(page.getByText("提供商选择已保存")).toBeVisible();

  await page.locator(".settingsGroupHead").filter({ hasText: "一致性资源库" }).click();
  const styleName = `E2E 冷调风格 ${Date.now()}`;
  const characterName = `E2E 角色 ${Date.now()}`;
  await page.getByPlaceholder("例如 冷调电影海报").fill(styleName);
  await page.getByRole("button", { name: "新建风格锁" }).click();
  await expect(page.locator(".resourceListItem strong").filter({ hasText: styleName })).toBeVisible();

  await page.getByPlaceholder("例如 白发女主").fill(characterName);
  await page.getByPlaceholder("例如 白色长发、细长凤眼、左眼下有泪痣").fill("稳定外观特征");
  await page.getByRole("button", { name: "新建角色档案" }).click();
  await expect(page.locator(".resourceListItem strong").filter({ hasText: characterName })).toBeVisible();
});

test("提示词库可新增、筛选和收藏提示词", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "提示词", exact: true }).click();

  const promptText = `E2E 提示词内容 ${Date.now()}`;
  await page.getByPlaceholder("写入一条常用提示词，只保存文字，不保存图片。").fill(promptText);
  await page.getByLabel("提示词模式").selectOption("generate");
  await page.getByRole("button", { name: "保存到库" }).click();
  await expect(page.getByText(promptText)).toBeVisible();

  await page.getByPlaceholder("搜索提示词").fill(promptText);
  const card = page.locator(".promptCard").filter({ hasText: promptText });
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "收藏" }).click();
  await expect(card.getByRole("button", { name: "已收藏" })).toBeVisible();
});

test("图库和历史页可打开筛选空态", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "历史", exact: true }).click();
  await expect(page.getByText("历史记录", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "图库", exact: true }).click();
  await expect(page.getByText(/历史图片按对话和时间保存|暂无图片|当前筛选下没有图片/).first()).toBeVisible();
});
