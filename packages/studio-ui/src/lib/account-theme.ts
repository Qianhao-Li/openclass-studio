import type { Theme } from "./profile-settings-state";

/** Keep account writes compatible with the deployed API and older web clients. */
export function encodeAccountTheme(theme: Theme): {
  theme: Exclude<Theme, "dark">;
  nightMode: boolean;
} {
  return {
    theme: theme === "dark" ? "system" : theme,
    nightMode: theme === "dark",
  };
}
