// The typed-name rule, kept pure so it can be tested without a browser: the confirm button
// enables only on an exact, non-empty match.
export function typedMatches(typed, name) {
  return typeof name === "string" && name.length > 0 && typed === name;
}
