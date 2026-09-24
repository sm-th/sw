// The site kit of every Smith Wiki research. `sw build` stages the research's
// pages together with the kit's layouts, section pages, and assets, then runs
// Eleventy with this configuration: --input is the staging directory.
const fs = require("node:fs");
const markdownIt = require("markdown-it");

module.exports = async function (eleventyConfig) {
  const { HtmlBasePlugin } = await import("@11ty/eleventy");
  // Root-absolute URLs in templates are rewritten under the Pages base path, such as /<research>/.
  eleventyConfig.addPlugin(HtmlBasePlugin);
  eleventyConfig.addPassthroughCopy("assets");
  eleventyConfig.setLibrary("md", markdownIt({ html: false, linkify: false, typographer: true }));

  eleventyConfig.addGlobalData("research", {
    name: process.env.SW_RESEARCH || "research",
    repository: process.env.SW_REPOSITORY_URL || "",
    version: process.env.SW_VERSION || "",
  });

  eleventyConfig.addFilter("machineDate", (value) => {
    const date = value instanceof Date ? value : new Date(value);
    return Number.isNaN(date.valueOf()) ? "" : date.toISOString();
  });

  eleventyConfig.addFilter("displayDate", (value) => {
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.valueOf())) return "";
    return new Intl.DateTimeFormat("en", { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" }).format(date);
  });

  eleventyConfig.addFilter("utcTime", (value) => {
    const date = value instanceof Date ? value : new Date(value);
    return Number.isNaN(date.valueOf()) ? "" : `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
  });

  eleventyConfig.addFilter("year", (value) => {
    const date = value instanceof Date ? value : new Date(value);
    return Number.isNaN(date.valueOf()) ? "" : String(date.getUTCFullYear());
  });

  // Pages whose Markdown links to the given page, read from their input files.
  const inputs = new Map();
  eleventyConfig.on("eleventy.before", () => inputs.clear());
  eleventyConfig.addFilter("citedBy", (items, url) => {
    const target = url.replace(/^\//, "");
    return items
      .filter((item) => item.url && item.url !== url && item.inputPath.endsWith(".md"))
      .filter((item) => {
        if (!inputs.has(item.inputPath)) inputs.set(item.inputPath, fs.readFileSync(item.inputPath, "utf8"));
        return inputs.get(item.inputPath).includes(target);
      })
      .sort((a, b) => String(a.data.title).localeCompare(String(b.data.title)));
  });

  eleventyConfig.addFilter("byTitle", (items) =>
    [...(items || [])].sort((a, b) => String(a.data.title).localeCompare(String(b.data.title))),
  );

  return {
    dir: { includes: "_includes" },
    pathPrefix: process.env.PATH_PREFIX || "/",
    htmlTemplateEngine: "njk",
    markdownTemplateEngine: false,
    templateFormats: ["md", "njk"],
  };
};
