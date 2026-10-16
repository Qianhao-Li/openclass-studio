const COURSE_PATH = /^\/api\/(lessons|packages|workspace|course-package|documents|board-assets|resources)(?:[/?]|$)/;

/** Every content request, stream, asset upload and socket uses the same isolation boundary. */
export function studioApiPath(path: string, learningSessionId?: string, repositoryId?: string, reviewSessionId?: string, version?: { repositoryId: string; versionId: string }) {
  if (version) {
    const localPath = path.replace(/^\/api\/repositories\/[^/]+\/studio(?=\/)/, "/api");
    if (COURSE_PATH.test(localPath)) return `/api/repositories/${encodeURIComponent(version.repositoryId)}/versions/${encodeURIComponent(version.versionId)}/studio${localPath.slice(4)}`;
  }
  if (reviewSessionId) {
    const localPath = path.replace(/^\/api\/repositories\/[^/]+\/studio(?=\/)/, "/api");
    if (COURSE_PATH.test(localPath)) return `/api/course-pr-reviews/${encodeURIComponent(reviewSessionId)}/studio${localPath}`;
  }
  if (learningSessionId && COURSE_PATH.test(path)) return `/api/learning-sessions/${encodeURIComponent(learningSessionId)}/studio${path}`;
  if (repositoryId && (/^\/api\/lessons\/[^/]+\/history\/nodes/.test(path) || /^\/api\/board-assets\//.test(path) || /^\/api\/packages\/[^/]+\/lessons\/[^/]+\/source-evidence\//.test(path))) return `/api/repositories/${encodeURIComponent(repositoryId)}/studio${path.slice(4)}`;
  return path;
}
