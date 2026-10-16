"use client";

import type { ReactNode } from "react";
import { createContext, useContext, useEffect, useMemo, useSyncExternalStore } from "react";

import {
  PROFILE_SETTINGS_CHANGED_EVENT,
  PROFILE_SETTINGS_STORAGE_KEY,
  readStoredProfileSettings,
  type InterfaceLanguage,
} from "../lib/profile-settings-state";
import { profileSettingsTexts } from "../lib/i18n/product-ui";

type InterfaceLanguageValue = {
  language: InterfaceLanguage;
  texts: ReturnType<typeof profileSettingsTexts>;
  /** `"zh-CN"` or `"en-US"` for Intl / toLocaleString */
  intlLocale: string;
  /** Matches `<html lang>` */
  htmlLang: string;
};

const InterfaceLanguageContext = createContext<InterfaceLanguageValue | null>(null);

function readLanguageFromDomStorage(): InterfaceLanguage {
  return readStoredProfileSettings().interfaceLanguage;
}

function subscribe(onStoreChange: () => void): () => void {
  function onCustom() {
    onStoreChange();
  }

  function onStorage(event: StorageEvent) {
    if (!event.key || event.key === PROFILE_SETTINGS_STORAGE_KEY) {
      onStoreChange();
    }
  }

  if (typeof window !== "undefined") {
    window.addEventListener(PROFILE_SETTINGS_CHANGED_EVENT, onCustom as EventListener);
    window.addEventListener("storage", onStorage);
  }

  return () => {
    if (typeof window !== "undefined") {
      window.removeEventListener(PROFILE_SETTINGS_CHANGED_EVENT, onCustom as EventListener);
      window.removeEventListener("storage", onStorage);
    }
  };
}

function getSnapshot(): InterfaceLanguage {
  if (typeof window !== "undefined") {
    readLanguageFromDomStorage();
  }
  return "en";
}

function getServerSnapshot(): InterfaceLanguage {
  return "en";
}

export function InterfaceLanguageProvider({ children }: { children: ReactNode }) {
  const language = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  useEffect(() => {
    document.documentElement.lang = "en";
  }, [language]);

  const value = useMemo<InterfaceLanguageValue>(() => {
    const texts = profileSettingsTexts(language);
    return {
      language,
      texts,
      intlLocale: texts.intlLocale,
      htmlLang: "en",
    };
  }, [language]);

  return (
    <InterfaceLanguageContext.Provider value={value}>{children}</InterfaceLanguageContext.Provider>
  );
}

export function useInterfaceLanguage(): InterfaceLanguageValue {
  const ctx = useContext(InterfaceLanguageContext);
  if (!ctx) {
    const language = getServerSnapshot();
    const texts = profileSettingsTexts(language);
    return {
      language,
      texts,
      intlLocale: texts.intlLocale,
      htmlLang: "en",
    };
  }
  return ctx;
}
