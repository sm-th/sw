// Data for the research card, index.md at the root of every research repository.
const DATE = /^\d{4}-\d{2}-\d{2}/;
const BRIEF_LIMIT = 280;

function text(value) {
  return typeof value === "string" && value.trim() !== "";
}

function problems(data) {
  const found = [];
  for (const key of ["title", "question", "brief"]) {
    if (!text(data[key])) found.push(`${key} is empty`);
  }
  if (text(data.brief) && Array.from(data.brief).length > BRIEF_LIMIT) {
    found.push(`brief has more than ${BRIEF_LIMIT} characters`);
  }
  const date = data.date instanceof Date ? data.date.toISOString() : String(data.date ?? "");
  if (!DATE.test(date)) found.push("date must be YYYY-MM-DD");
  return found;
}

module.exports = {
  layout: "layouts/research.njk",
  templateEngineOverride: "md",
  permalink: "/index.html",
  eleventyExcludeFromCollections: true,
  eleventyComputed: {
    cardChecked(data) {
      const found = problems(data);
      if (found.length) throw new Error(`Invalid research card index.md:\n- ${found.join("\n- ")}`);
      return true;
    },
  },
};
