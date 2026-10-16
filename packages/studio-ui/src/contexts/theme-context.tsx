"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import {
  DISPLAY_THEME_CHANGED_EVENT,
  PROFILE_SETTINGS_CHANGED_EVENT,
  PROFILE_SETTINGS_STORAGE_KEY,
  readStoredProfileSettings,
  storeDisplayTheme,
  type ProfileSettings,
  type Theme,
} from "../lib/profile-settings-state";

type ThemeValue = {
  theme: Theme;
  themeSaveFailed: boolean;
  setTheme: (theme: Theme) => void;
};

const ThemeContext = createContext<ThemeValue | null>(null);

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.removeAttribute("data-night-mode");
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setCurrentTheme] = useState<Theme>("system");
  const [themeSaveFailed, setSaveFailed] = useState(false);

  useEffect(() => {
    function sync(next: Theme) {
      applyTheme(next);
      setCurrentTheme(next);
      setSaveFailed(false);
    }

    function onSettingsChanged(event: Event) {
      const settings = (event as CustomEvent<ProfileSettings>).detail;
      sync((settings ?? readStoredProfileSettings()).theme);
    }

    function onThemeChanged(event: Event) {
      sync((event as CustomEvent<Theme>).detail);
    }

    function onStorage(event: StorageEvent) {
      if (event.key === null || event.key.startsWith(PROFILE_SETTINGS_STORAGE_KEY)) {
        sync(readStoredProfileSettings().theme);
      }
    }

    sync(readStoredProfileSettings().theme);
    window.addEventListener(PROFILE_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    window.addEventListener(DISPLAY_THEME_CHANGED_EVENT, onThemeChanged);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(PROFILE_SETTINGS_CHANGED_EVENT, onSettingsChanged);
      window.removeEventListener(DISPLAY_THEME_CHANGED_EVENT, onThemeChanged);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  function setTheme(next: Theme) {
    applyTheme(next);
    setCurrentTheme(next);
    setSaveFailed(!storeDisplayTheme(next));
  }

  return (
    <ThemeContext.Provider value={{ theme, themeSaveFailed, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("useTheme must be used within ThemeProvider");
  return context;
}
