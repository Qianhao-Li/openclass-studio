export const INTERFACE_LANGUAGE_OPTIONS = [
  { value: "en", label: "English" },
] as const;

export type InterfaceLanguage = "en" | "zh-CN";

export type Theme = "system" | "light" | "dark" | "warm";

export type ProfileSettings = {
  displayName: string;
  handle: string;
  profileVisibility: "private" | "workspace" | "public";
  publicEmail: string;
  showPublicEmail: boolean;
  showSocialLinks: boolean;
  showRepositoriesOnProfile: boolean;
  showStarsOnProfile: boolean;
  bio: string;
  website: string;
  company: string;
  location: string;
  learningFocus: string;
  socialLinks: string[];
  theme: Theme;
  density: "comfortable" | "compact";
  startPage: "home" | "studio" | "profile";
  /** UI locale; changing it updates `<html lang>` via InterfaceLanguageProvider. */
  interfaceLanguage: InterfaceLanguage;
  reduceMotion: boolean;
  highContrast: boolean;
  largeText: boolean;
  visibleFocus: boolean;
  courseActivityNotifications: boolean;
  weeklyDigestNotifications: boolean;
  aiResultNotifications: boolean;
  resourceNotifications: boolean;
  browserNotifications: boolean;
  notificationFrequency: "instant" | "hourly" | "daily";
  quietStart: string;
  quietEnd: string;
  emailCourseDigest: boolean;
  emailAiSummary: boolean;
  emailSecurityAlerts: boolean;
  preferredTextModel: string;
  preferred_codex_connection_id: string | null;
  preferredRealtimeModel: string;
  autoSourceRetrieval: boolean;
  hideLocalPaths: boolean;
  confirmExternalLinks: boolean;
  clearSessionOnExit: boolean;
  allowCourseDiscovery: boolean;
};

export const PROFILE_SETTINGS_STORAGE_KEY = "openclass.profile.settings";
export const PROFILE_SETTINGS_CHANGED_EVENT =
  "openclass.profile.settings.changed";
export const DISPLAY_THEME_CHANGED_EVENT = "openclass.display.theme.changed";

export const DEFAULT_PROFILE_SETTINGS: ProfileSettings = {
  displayName: "",
  handle: "",
  profileVisibility: "workspace",
  publicEmail: "",
  showPublicEmail: false,
  showSocialLinks: true,
  showRepositoriesOnProfile: true,
  showStarsOnProfile: true,
  bio: "",
  website: "",
  company: "",
  location: "",
  learningFocus: "",
  socialLinks: ["", "", "", ""],
  theme: "system",
  density: "comfortable",
  startPage: "home",
  interfaceLanguage: "en",
  reduceMotion: false,
  highContrast: false,
  largeText: false,
  visibleFocus: true,
  courseActivityNotifications: true,
  weeklyDigestNotifications: true,
  aiResultNotifications: true,
  resourceNotifications: false,
  browserNotifications: false,
  notificationFrequency: "instant",
  quietStart: "22:00",
  quietEnd: "08:00",
  emailCourseDigest: true,
  emailAiSummary: false,
  emailSecurityAlerts: true,
  preferredTextModel: "auto",
  preferred_codex_connection_id: null,
  preferredRealtimeModel: "auto",
  autoSourceRetrieval: false,
  hideLocalPaths: true,
  confirmExternalLinks: true,
  clearSessionOnExit: false,
  allowCourseDiscovery: false,
};

export const PROFILE_SETTINGS_ACTIVE_ACCOUNT_KEY = `${PROFILE_SETTINGS_STORAGE_KEY}.account`;

function parseCachedSettings(value: string | null): Partial<ProfileSettings> & { nightMode?: boolean } | null {
  try {
    return value ? JSON.parse(value) : null;
  } catch {
    return null;
  }
}

/** Validate caches as untrusted input; never restore legacy shared profile identity. */
export function normalizeProfileSettings(
  raw: (Partial<ProfileSettings> & { nightMode?: boolean }) | null | undefined,
): ProfileSettings {
  const next = {
    ...DEFAULT_PROFILE_SETTINGS,
    socialLinks: [...DEFAULT_PROFILE_SETTINGS.socialLinks],
  };
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return next;
  for (const key of Object.keys(
    DEFAULT_PROFILE_SETTINGS,
  ) as (keyof ProfileSettings)[]) {
    const value = raw[key];
    if (
      typeof value === typeof DEFAULT_PROFILE_SETTINGS[key] &&
      key !== "socialLinks"
    ) {
      Object.assign(next, { [key]: value });
    }
  }
  next.theme = ["system", "light", "dark", "warm"].includes(next.theme)
    ? next.theme
    : "system";
  if (raw.nightMode === true) next.theme = "dark";
  next.density = next.density === "compact" ? "compact" : "comfortable";
  next.preferred_codex_connection_id = typeof raw.preferred_codex_connection_id === "string" && raw.preferred_codex_connection_id.trim()
    ? raw.preferred_codex_connection_id.trim() : null;
  next.interfaceLanguage = "en";
  next.socialLinks = Array.from({ length: 4 }, (_, index) =>
    typeof raw.socialLinks?.[index] === "string" ? raw.socialLinks[index] : "",
  );
  return next;
}

export function profileSettingsStorageKey(userId: string): string {
  return `${PROFILE_SETTINGS_STORAGE_KEY}.${userId}`;
}

export function activateProfileSettingsAccount(userId: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (userId)
      window.localStorage.setItem(PROFILE_SETTINGS_ACTIVE_ACCOUNT_KEY, userId);
    else window.localStorage.removeItem(PROFILE_SETTINGS_ACTIVE_ACCOUNT_KEY);
  } catch {
    /* Server settings remain authoritative when local storage is unavailable. */
  }
  window.dispatchEvent(
    new CustomEvent(PROFILE_SETTINGS_CHANGED_EVENT, {
      detail: readStoredProfileSettings(userId),
    }),
  );
}

export function cacheAccountSettings(
  userId: string,
  settings: Partial<ProfileSettings>,
): boolean {
  if (typeof window === "undefined") return false;
  const next = normalizeProfileSettings(settings);
  let cached = true;
  let active: string | null = null;
  try {
    window.localStorage.setItem(
      profileSettingsStorageKey(userId),
      JSON.stringify(next),
    );
    active = window.localStorage.getItem(PROFILE_SETTINGS_ACTIVE_ACCOUNT_KEY);
    if (active === userId) {
      // This shared key contains only the device display preference, never an account profile.
      const device = parseCachedSettings(
        window.localStorage.getItem(PROFILE_SETTINGS_STORAGE_KEY),
      );
      if (!deviceTheme(device)) {
        window.localStorage.setItem(
          PROFILE_SETTINGS_STORAGE_KEY,
          JSON.stringify({ theme: next.theme }),
        );
      }
    }
  } catch {
    cached = false;
  }
  if (active === userId) {
    window.dispatchEvent(
      new CustomEvent(PROFILE_SETTINGS_CHANGED_EVENT, {
        detail: readStoredProfileSettings(userId),
      }),
    );
  }
  return cached;
}

export function resetProfileSettingsCache(userId?: string): void {
  if (typeof window === "undefined") return;
  try {
    if (userId)
      window.localStorage.removeItem(profileSettingsStorageKey(userId));
    window.localStorage.removeItem(PROFILE_SETTINGS_ACTIVE_ACCOUNT_KEY);
  } catch {
    /* Local cache cleanup is best effort. */
  }
  window.dispatchEvent(
    new CustomEvent(PROFILE_SETTINGS_CHANGED_EVENT, {
      detail: readStoredProfileSettings(null),
    }),
  );
}

function deviceTheme(raw: unknown): Theme | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const settings = raw as { theme?: unknown; nightMode?: unknown };
  if (settings.nightMode === true) return "dark";
  return ["system", "light", "dark", "warm"].includes(settings.theme as string)
    ? settings.theme as Theme
    : undefined;
}

/** Write only display preferences, never unsaved identity or settings drafts. */
export function storeDisplayTheme(theme: Theme): boolean {
  let saved = true;
  try {
    window.localStorage.setItem(PROFILE_SETTINGS_STORAGE_KEY, JSON.stringify({ theme }));
  } catch {
    saved = false;
  }
  window.dispatchEvent(new CustomEvent(DISPLAY_THEME_CHANGED_EVENT, { detail: theme }));
  return saved;
}

// Apply the device display preference before hydration to avoid a bright flash.
export const THEME_INITIALIZATION_SCRIPT = `try {
  var read = function(key) { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; } };
  var device = read(${JSON.stringify(PROFILE_SETTINGS_STORAGE_KEY)});
  var account = localStorage.getItem(${JSON.stringify(PROFILE_SETTINGS_ACTIVE_ACCOUNT_KEY)});
  var scoped = account ? read(${JSON.stringify(PROFILE_SETTINGS_STORAGE_KEY)} + "." + account) : null;
  var valid = function(value) { return ["system", "light", "dark", "warm"].includes(value); };
  var theme = device?.nightMode === true ? "dark" : valid(device?.theme) ? device.theme : scoped?.nightMode === true ? "dark" : valid(scoped?.theme) ? scoped.theme : "system";
  document.documentElement.dataset.theme = theme;
} catch { document.documentElement.dataset.theme = "system"; }`;

export function readStoredProfileSettings(
  userId?: string | null,
): ProfileSettings {
  if (typeof window === "undefined") return normalizeProfileSettings(null);
  try {
    const accountId =
      userId === undefined
        ? window.localStorage.getItem(PROFILE_SETTINGS_ACTIVE_ACCOUNT_KEY)
        : userId;
    const scoped = accountId
      ? window.localStorage.getItem(profileSettingsStorageKey(accountId))
      : null;
    const settings = normalizeProfileSettings(parseCachedSettings(scoped));
    const device = parseCachedSettings(
      window.localStorage.getItem(PROFILE_SETTINGS_STORAGE_KEY),
    );
    return {
      ...settings,
      theme: deviceTheme(device) ?? settings.theme,
    };
  } catch {
    return normalizeProfileSettings(null);
  }
}
