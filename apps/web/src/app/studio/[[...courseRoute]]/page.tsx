import { LocalStudio } from "@openclass/studio/components/local-studio";
export const dynamic = "force-dynamic";
export default function Studio() { return <LocalStudio launchToken={process.env.OPENCLASS_STUDIO_LAUNCH_TOKEN || ""} apiOrigin={process.env.OPENCLASS_STUDIO_API_ORIGIN || "http://127.0.0.1:8510"} />; }
