import type { Metadata } from "next";
import { InterfaceLanguageProvider } from "@openclass/studio/contexts/interface-language-context";
import { ThemeProvider } from "@openclass/studio/contexts/theme-context";
import { THEME_INITIALIZATION_SCRIPT } from "@openclass/studio/lib/profile-settings-state";
import "@openclass/studio/styles";
import "@openclass/studio/components/course-studio/word-editor-night.css";
import "katex/dist/katex.min.css";
export const metadata: Metadata = { title: "OpenClass Studio", description: "An independent course IDE", other: { google: "notranslate" } };
export default function Layout({ children }: { children: React.ReactNode }) {
return <html lang="en" translate="no" suppressHydrationWarning><head><script dangerouslySetInnerHTML={{ __html: THEME_INITIALIZATION_SCRIPT }} /></head><body><ThemeProvider><InterfaceLanguageProvider>{children}</InterfaceLanguageProvider></ThemeProvider></body></html>;
}
