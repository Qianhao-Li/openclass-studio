import type { UserView } from ".";
import type { ProfileSettings } from "../lib/profile-settings-state";

export const ACCOUNT_SETTINGS_FIELDS = [
  "displayName",
  "handle",
  "bio",
  "website",
  "company",
  "location",
  "learningFocus",
  "socialLinks",
  "showSocialLinks",
  "showRepositoriesOnProfile",
  "showStarsOnProfile",
  "theme",
  "density",
  "reduceMotion",
  "highContrast",
  "largeText",
  "visibleFocus",
  "preferredTextModel",
  "preferred_codex_connection_id",
  "preferredRealtimeModel",
  "autoSourceRetrieval",
] as const;

export type AccountSettings = Omit<
  Pick<ProfileSettings, (typeof ACCOUNT_SETTINGS_FIELDS)[number]>,
  "theme"
> & {
  theme: Exclude<ProfileSettings["theme"], "dark">;
  nightMode: boolean;
};
export type AccountSettingsResponse = {
  settings: AccountSettings;
  revision: number;
  user: UserView;
};
