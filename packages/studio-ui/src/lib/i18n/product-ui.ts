import type { InterfaceLanguage } from "../profile-settings-state";

function intl(_locale: InterfaceLanguage): string {
  return "en-US";
}

export function uiIntlLocale(interfaceLanguage: InterfaceLanguage): string {
  return intl(interfaceLanguage);
}

export type ProfileSettingsTexts = ReturnType<typeof profileSettingsTexts>;

export function profileSettingsTexts(_lang: InterfaceLanguage) {
  const lang: InterfaceLanguage = "en";
  const z = false;
  return {
    intlLocale: intl(lang),
    lang,

    nav: {
      primary: [
        { id: "profile" as const, label: z ? "公开资料" : "Public profile" },
        { id: "account" as const, label: z ? "账户" : "Account" },
        { id: "appearance" as const, label: z ? "外观" : "Appearance" },
        { id: "notifications" as const, label: z ? "通知" : "Notifications" },
      ],
      account: [
        { id: "billing" as const, label: z ? "计费和许可" : "Billing & license" },
        { id: "email" as const, label: z ? "电子邮件" : "Email" },
        { id: "password" as const, label: z ? "密码和身份验证" : "Password & auth" },
        { id: "models" as const, label: z ? "AI 模型" : "AI models" },
        { id: "connections" as const, label: z ? "已连接账号" : "Connected accounts" },
        { id: "security" as const, label: z ? "代码安全" : "Security" },
      ],
      entitlementLabel: z ? "使用权" : "Plan",
      navAria: z ? "个人设置导航" : "Profile settings navigation",
    },

    sectionEyebrows: {
      profile: z ? "Profile" : "Profile",
      account: z ? "Account" : "Account",
      appearance: z ? "Appearance" : "Appearance",
      notifications: z ? "Notifications" : "Notifications",
      billing: z ? "License" : "License",
      email: z ? "Email" : "Email",
      password: z ? "Authentication" : "Authentication",
      models: z ? "Models" : "Models",
      connections: z ? "Connections" : "Connections",
      security: z ? "Security" : "Security",
    },

    sectionTitles: {
      profile: z ? "公开资料" : "Public profile",
      account: z ? "账户概览" : "Account overview",
      appearance: z ? "外观" : "Appearance",
      notifications: z ? "通知" : "Notifications",
      billing: z ? "计费和许可" : "Billing & licensing",
      email: z ? "电子邮件" : "Email",
      password: z ? "密码和身份验证" : "Password & authentication",
      models: z ? "AI 模型" : "AI models",
      connections: z ? "已连接账号" : "Connected accounts",
      security: z ? "代码安全" : "Security",
    },

    appearance: {
      themeDescription: z ? "主题在整个 OpenClass 中立即生效，并保存在此浏览器。保存设置可同步到账户。下方的阅读偏好应用于个人主页和设置。" : "Your theme applies immediately across OpenClass and is saved in this browser. Save settings to sync it to your account. The reading preferences below apply to your profile pages and settings.",
      themeSaveFailed: z ? "主题已生效，但此浏览器无法保存。你仍可将设置保存到账户。" : "Your theme has been applied, but this browser could not save it. You can still save settings to your account.",
      interfaceLanguageLabel: z ? "界面语言" : "Interface language",
      theme: z ? "主题" : "Theme",
      themeSystem: z ? "跟随系统" : "Follow system",
      themeLight: z ? "白天" : "Day",
      themeDark: z ? "黑夜" : "Night",
      themeWarm: z ? "暖色纸面" : "Warm paper",
      density: z ? "界面密度" : "Density",
      densityComfortable: z ? "舒展" : "Comfortable",
      densityCompact: z ? "紧凑" : "Compact",
      startPage: z ? "默认入口" : "Start page",
      startHome: z ? "学习首页" : "Home",
      startStudio: z ? "课程工作台" : "Studio",
      startProfile: z ? "个人主页" : "Profile",
      readingAssistTitle: z ? "阅读辅助" : "Reading assist",
      readingAssistSubtitle: z ? "这些选项会立即作用于个人主页和设置页。" : "These apply immediately on profile and settings.",
      resetAppearance: z ? "重置外观" : "Reset appearance",
      reduceMotion: z ? "减少动态效果" : "Reduce motion",
      reduceMotionDesc: z ? "降低动画和过渡频率。" : "Fewer animations and transitions.",
      highContrast: z ? "高对比度" : "High contrast",
      highContrastDesc: z ? "提高文字和边框对比度。" : "Stronger text and border contrast.",
      largeText: z ? "放大正文" : "Larger text",
      largeTextDesc: z ? "提高课程列表和设置页正文尺寸。" : "Increase body text size where supported.",
      visibleFocus: z ? "突出键盘焦点" : "Visible focus rings",
      visibleFocusDesc: z ? "让键盘导航状态更容易被看见。" : "Make keyboard focus easier to see.",
      saveHelper: z ? "外观会立即预览，保存后下次打开仍然保留。" : "Preview updates live; Save keeps them for next visit.",
      previewTagAi: z ? "AI 课程" : "AI course",
      previewTagLesson: z ? "讲义" : "Lesson",
      livePreview: "Live Preview",
    },

    save: {
      button: z ? "保存设置" : "Save settings",
      saved: z ? "已保存到本机" : "Saved locally",
      saveFail: z ? "保存失败，请检查浏览器存储权限。" : "Could not save. Check browser storage permissions.",
    },

    home: {
      brandSubtitle: z ? "AI 课程工作台" : "AI course workspace",
      brandTitle: z ? "开放课堂" : "OpenClass",
      coursePackages: z ? "课程包" : "Course packages",
      addPackageAria: z ? "添加课程包" : "Add course package",
      createCoursePackage: z ? "新建课程包" : "Create course package",
      importCourseFolder: z ? "导入课程文件夹" : "Import course folder",
      importCourseFolderHint: z ? "识别文件夹及子文件夹中的 .ridoc 课程包，其他文件不会上传。" : "Import .ridoc course packages from this folder and its subfolders. Other files will not be uploaded.",
      importCourseFolderEmpty: z ? "此文件夹及子文件夹中未找到 .ridoc 文件。" : "No .ridoc files were found in this folder or its subfolders.",
      importCourseFolderUnsupported: z ? "当前浏览器不支持选择文件夹，请使用支持文件夹选择的浏览器。" : "This browser does not support folder selection. Please use a browser with folder selection support.",
      importCourseFolderProgress: (completed: number, total: number) => z ? `正在导入课程包：${completed}/${total}` : `Importing course packages: ${completed}/${total}`,
      importCourseFolderResult: (imported: number, skipped: number, failed: number) => z ? `已导入 ${imported} 个课程包，跳过 ${skipped} 个非 RIDOC 文件，失败 ${failed} 个。` : `Imported ${imported} course packages. Skipped ${skipped} non-RIDOC files. Failed: ${failed}.`,
      importCourseFolderWait: z ? "请等待导入完成" : "Please wait for import to finish",
      importCourseFolderClose: z ? "关闭" : "Close",
      collapseCoursePackages: z ? "收起课程包" : "Collapse course packages",
      expandCoursePackages: z ? "展开课程包" : "Expand course packages",
      standaloneLessons: z ? "单独课程" : "Standalone lessons",
      standaloneHint: z ? "默认仅显示未入包课程" : "Unassigned lessons only",
      batchManage: z ? "批量管理" : "Manage",
      batchCancel: z ? "取消" : "Cancel",
      batchSelected: (count: number) => (z ? `已选 ${count} 项` : `${count} selected`),
      batchSelectAll: z ? "全选当前列表" : "Select visible",
      batchClear: z ? "清空" : "Clear",
      batchChoosePackage: z ? "选择课程包" : "Choose package",
      batchMove: z ? "移动" : "Move",
      batchDelete: z ? "批量删除" : "Delete selected",
      batchDeleteConfirm: (count: number) =>
        z
          ? `确定删除选中的 ${count} 节课程吗？删除后可在个人账户的“最近删除”中恢复。`
          : `Delete the ${count} selected lesson${count === 1 ? "" : "s"}? You can restore them from Recently deleted in your account.`,
      batchMoveFail: z ? "批量移动课程失败" : "Could not move selected lessons",
      batchDeleteFail: z ? "批量删除课程失败" : "Could not delete selected lessons",
      packageManageRenameHint: z ? "选择一节课程后可重命名" : "Select one lesson to rename",
      packageManageWorking: z ? "正在更新课程，请稍候" : "Updating lessons, please wait",
      packageManageDeleteUnconfirmed: (message: string) => z ? `删除已停止，无法确认最终结果，请刷新课程列表。${message}` : `Deletion stopped. The final result could not be confirmed; refresh the lesson list. ${message}`,
      packageManageDeleteProgress: (completed: number, total: number) => z ? `已删除 ${completed}/${total} 节课程` : `Deleted ${completed}/${total} lessons`,
      packageManageDeleteStopped: (completed: number, total: number, message: string) => z ? `删除已停止：已删除 ${completed}/${total} 节课程，其余保留。${message}` : `Deletion stopped: ${completed}/${total} lessons deleted; the rest were kept. ${message}`,
      collapseStandaloneLessons: z ? "收起单独课程" : "Collapse standalone lessons",
      expandStandaloneLessons: z ? "展开单独课程" : "Expand standalone lessons",
      standaloneWorkspaceAria: z ? "进入单独课程工作台" : "Open standalone workspace",
      standaloneCreateMenuAria: z ? "添加单独课程" : "Add standalone lesson",
      importCourseFile: z ? "导入课程文件" : "Import course file",
      createCourse: z ? "新建课程" : "Create course",
      packageSelectedExpanded: z ? "已选中，右侧正在展示包内单课；再点可取消选中。" : "Selected — lessons shown on the right; click again to collapse.",
      packageSelectedCollapsed: z ? "已选中，再点可展开包内单课列表。" : "Selected — click again to expand lessons.",
      emptyPackage: z ? "空课程包，点一下先选中它。" : "Empty package — click to select.",
      noPackages: z ? "还没有课程包，点击右上角加号新建课程包或导入课程文件夹。" : "No packages yet — use + to create a package or import a course folder.",
      packageNameLabel: z ? "课程包名称" : "Package name",
      packageNamePlaceholder: z ? "输入课程包名称" : "Package title",
      lessonMenuAria: z ? "打开课程操作菜单" : "Lesson actions menu",
      lessonMoreTitle: z ? "更多操作" : "More actions",
      publicationReviewScanning: z ? "资料扫描中" : "Scanning materials",
      publicationReviewProgressAria: z ? "课程发布扫描进度" : "Course publication scan progress",
      publicationReviewCheckingReferences: z ? "正在核对课程引用范围" : "Checking referenced materials",
      publicationReviewCheckingReferencesDetail: z ? "正在定位课程实际引用的资料" : "Locating materials actually referenced by this course",
      publicationReviewReadingSources: z ? "正在读取原始资料" : "Reading original materials",
      publicationReviewReadingSourcesDetail: (completed: number, total: number) =>
        z ? `已读取 ${completed}/${total} 份资料` : `Read ${completed}/${total} materials`,
      publicationReviewReviewingUnits: z ? "AI 正在审查资料片段" : "AI is reviewing material sections",
      publicationReviewReviewingUnitsDetail: (
        completed: number,
        total: number,
        batchIndex: number,
        batchCount: number,
      ) =>
        z
          ? `第 ${batchIndex}/${batchCount} 批 · 已完成 ${completed}/${total} 个片段`
          : `Batch ${batchIndex}/${batchCount} · ${completed}/${total} sections completed`,
      publicationReviewVerifyingSources: z ? "正在确认资料未发生变化" : "Verifying materials are unchanged",
      publicationReviewVerifyingSourcesDetail: (total: number) =>
        z ? `已完成 ${total} 个片段的审查，正在做最终一致性检查` : `${total} sections reviewed; running the final consistency check`,
      noLessonMatch: z ? "还没有匹配到课程。试试换个关键词，或者去工作台创建一节新课。" : "No matching lessons — try another keyword or create one in Studio.",
      noStandalone: z ? "现在没有未被存入课程包的单独课程。你可以先新建课程，或者把包内课程移回单独课程池。" : "No standalone lessons yet — create one or move lessons out of packages.",
      loadError: z ? "加载主页数据失败" : "Could not load home data",
      lessonOpenError: z ? "打开课程失败" : "Could not open lesson",
      standaloneFallbackTitle: z ? "单独课程" : "Standalone",
      sharePackageFail: z ? "分享课程包失败" : "Could not share package",
      openStandaloneFail: z ? "打开单独课程工作台失败" : "Could not open standalone workspace",
      moveLessonFail: z ? "移动课程失败" : "Could not move lesson",
      renameLessonFail: z ? "重命名课程失败" : "Could not rename lesson",
      deleteLessonFail: z ? "删除课程失败" : "Could not delete lesson",
      exportLessonPackageFail: z ? "导出课程包失败" : "Could not export lesson package",
      loadLessonPackageFail: z ? "加载课程包失败" : "Could not load lesson package",
      createPackageFail: z ? "新建课程包失败" : "Could not create package",
      openPackageFail: z ? "打开课程包失败" : "Could not open package",
      renamePackageFail: z ? "重命名课程包失败" : "Could not rename package",
      deletePackageFail: z ? "删除课程包失败" : "Could not delete package",
      searchPlaceholder: z ? "搜索别人的开源课程、作者、主题或知识方向..." : "Search open courses, authors, topics...",
      activityTitle: z ? "学习活跃度" : "Learning activity",
      activitySubtitle: z ? "过去 52 周内课程编辑、提交与资料接入的活动分布。" : "Edits, commits, and uploads over the past 52 weeks.",
      activityTotal: (count: number) =>
        z
          ? `${count.toLocaleString(intl(lang))} 次活动`
          : `${count.toLocaleString(intl(lang))} activities`,
      activityDayTitle: (date: string, count: number) =>
        z ? `${date} · ${count} 次活动` : `${date} · ${count} activities`,
      lastActivePrefix: z ? "最近一次活跃：" : "Last active: ",
      noActivityYet: z ? "暂无记录" : "No activity yet",
      feedCollapseAria: z ? "收起 Feed" : "Collapse feed",
      feedExpandAria: z ? "展开 Feed" : "Expand feed",
      feedSubtitle: z ? "最近的课程提交和工作台推进会按时间排在这里。" : "Recent commits and studio updates appear here.",
      feedEmpty: z ? "还没有可以展示的更新。新建课程或编辑文稿后，这里会自动变成最近活动流。" : "Nothing here yet — edits will appear in this feed.",
      feedTimelineSummary: (count: number) =>
        z
          ? `${count.toLocaleString(intl(lang))} 条修改记录`
          : `${count.toLocaleString(intl(lang))} change${count === 1 ? "" : "s"}`,
      feedTimelineLatestLabel: z ? "最近一次" : "Latest",
      feedTimelineExpand: (count: number) =>
        z
          ? `展开 ${count.toLocaleString(intl(lang))} 条修改记录`
          : `Show ${count.toLocaleString(intl(lang))} change${count === 1 ? "" : "s"}`,
      feedTimelineCollapse: z ? "收起修改记录" : "Hide changes",
      feedReadMore: z ? "查看详情" : "Read more",
      notificationToggleAria: z ? "切换消息面板" : "Toggle notifications",
      languageSwitchToEnglish: z ? "切换到英语界面" : "Switch to English",
      languageSwitchToChinese: z ? "切换到中文界面" : "Switch to Chinese",
      filterAll: z ? "全部" : "All",
      filterMine: z ? "我的" : "Mine",
      filterTrending: z ? "热门" : "Popular",
      updatedLabel: z ? "更新" : "Updated",
      shareLater: z ? "分享功能稍后提供" : "Sharing coming soon",
      share: z ? "分享" : "Share",
      renameLesson: z ? "重命名" : "Rename",
      renameLessonPrompt: z ? "请输入新的课程名称" : "Enter a new lesson name",
      renamePackage: z ? "重命名" : "Rename",
      currentPackage: z ? "当前课程包" : "Current package",
      deletePackageTitle: z ? "删除课程包" : "Delete package",
      sharePackageTitle: z ? "分享课程包" : "Share package",
      shareAfterPublic: z ? "设为公开后即可分享" : "Make it public before sharing",
      pushToRepository: z ? "Push" : "Push",
      syncRepository: z ? "Sync" : "Sync",
      repositorySynced: z ? "Synced" : "Synced",
      repositorySection: z ? "Push" : "Push",
      repositoryHintPush: z
        ? "Push 当前版本到个人仓库（默认私密）。可在 Repositories 中改为公开。"
        : "Push the current version to your personal repository (private by default). Make it public from Repositories.",
      repositoryHintSync: z
        ? "本地有新更改，再 Push 才会更新仓库远端副本。"
        : "Local changes are ahead; Push again to update the remote copy.",
      repositoryHintSynced: z
        ? "仓库已是最新。公开/私密请在 Repositories 中管理。"
        : "Repository is up to date. Manage public/private in Repositories.",
      pushRepositoryFail: z ? "Push 失败" : "Could not push to repository",
      renamePackageTitle: z ? "重命名课程包" : "Rename package",
      collapsePackageLessons: z ? "收起单课列表" : "Collapse lesson list",
      packageLessonCount: (count: number) => (z ? `${count} 课` : `${count} lesson${count === 1 ? "" : "s"}`),
      lessonSummaryFallback: z ? "已创建课程文档，等待继续补充内容。" : "Lesson document created; ready for more content.",
      selectedPackageEmpty: z
        ? "这个课程包还是空的，先把课程移动进来，或者进入工作台新建一页。"
        : "This package is empty. Move a lesson into it, or open Studio to create the first page.",
      moveToPackage: z ? "移动到课程包" : "Move to package",
      exportLessonPackage: z ? "导出课程包" : "Export lesson package",
      delete: z ? "删除" : "Delete",
      noMovablePackages: z ? "暂无可移动课程包" : "No packages to move into",
      moreUpdatesAria: z ? "更多更新操作" : "More updates",
    },

    homeRelative: {
      justNow: z ? "刚刚" : "just now",
      minutesAgo: (n: number) => (z ? `${n} 分钟前` : `${n}m ago`),
      hoursAgo: (n: number) => (z ? `${n} 小时前` : `${n}h ago`),
      daysAgo: (n: number) => (z ? `${n} 天前` : `${n}d ago`),
    },

    studio: {
      loading: z ? "正在载入课程工作台…" : "Loading course studio…",
      packageMissing: z ? "没有找到可用课程。" : "No available course was found.",
      returnHome: z ? "返回主页" : "Back to home",
      expandRightSidebar: z ? "展开右侧栏" : "Expand right sidebar",
      collapseRightSidebar: z ? "收起右侧栏" : "Collapse right sidebar",
      expandTopToolbar: z ? "展开顶部与编辑工具栏" : "Expand top and editor toolbar",
      collapseTopToolbar: z ? "收起顶部与编辑工具栏" : "Collapse top and editor toolbar",
      closeErrorAria: z ? "关闭错误提示" : "Dismiss error message",
      closeErrorTitle: z ? "关闭提示" : "Dismiss",
      confirm: z ? "确认" : "Confirm",
      cancel: z ? "取消" : "Cancel",
      createPageTitle: z ? "新建页面" : "Create page",
      newPageNameLabel: z ? "新页面名称" : "New page name",
      firstPageNameLabel: z ? "第一页名称" : "First page name",
      lessonNamePlaceholder: z ? "课程导读 / 第一讲 / 练习讲义" : "Course intro / Lecture 1 / Practice notes",
      emptyPackageTitle: z ? "这个课程包还是空的" : "This package is empty",
      emptyPackageBody: z
        ? "创建后可以直接和 AI 对话；页面标题会在完成首轮对话后自动概括。"
        : "Create the page and start chatting with AI. Its title will be summarized automatically after the first completed exchange.",
      createFirstPage: z ? "新建第一页" : "Create first page",
    },

    auth: {
      checking: z ? "正在检查登录状态" : "Checking sign-in status",
      noAdminPermission: z ? "当前账号没有管理员权限。" : "Your account does not have admin access.",
    },

    accountMenu: {
      loadingAccount: z ? "正在读取账号" : "Loading account…",
      guestBadge: z ? "游客模式" : "Guest",
      adminBadge: z ? "管理员" : "Admin",
      memberBadge: z ? "普通用户" : "Member",
      loginToSave: z ? "登录以保存" : "Sign in to save",
      profileLink: z ? "个人账号" : "Account",
      adminLink: z ? "管理员后台" : "Admin",
      signOutGuest: z ? "结束游客访问" : "End guest session",
      signOut: z ? "退出登录" : "Sign out",
    },

    profileHome: {
      backToBrand: z ? "开放课堂" : "OpenClass",
      workspace: z ? "工作台" : "Studio",
      loading: z ? "正在加载个人主页…" : "Loading profile…",
      loadWorkspaceError: z ? "加载个人项目失败" : "Could not load your projects",
      avatarAlt: z ? "开放课堂用户头像" : "OpenClass profile avatar",
      noUpdatesYet: z ? "暂无更新" : "No updates yet",
      tabSettings: z ? "个人设置" : "Settings",
      tabRepositories: z ? "项目" : "Repositories",
      tabCollaboration: z ? "协作" : "Collaboration",
      tabStars: z ? "收藏" : "Stars",
      repoFilterAll: z ? "全部" : "All",
      repoFilterLessons: z ? "单独课程" : "Lessons",
      repoFilterPackages: z ? "课程包" : "Packages",
      uploadRidoc: z ? "上传 .ridoc" : "Upload .ridoc",
      uploadingRidoc: z ? "正在上传…" : "Uploading…",
      ridocImportFail: z ? "上传 RIDOC 课程失败" : "Could not upload the RIDOC course",
      updated: z ? "更新" : "Updated",
      lessonOpenError: z ? "打开课程失败" : "Could not open lesson",
      navAria: z ? "个人主页内容导航" : "Profile navigation",
      visibilitySection: z ? "仓库可见性" : "Repository visibility",
      visibilityPublic: z ? "公开" : "Public",
      visibilityPrivate: z ? "私密" : "Private",
      visibilityHintPublic: z
        ? "公开后可被搜索、收藏与分享；再次同步本地更改才会更新公开内容。"
        : "Public items can be discovered, starred, and shared. Sync again to update the public snapshot.",
      visibilityHintPrivate: z
        ? "私密仓库仅自己可见，不会出现在公开发现中。"
        : "Private repository items are only visible to you and stay out of public discovery.",
      badgePublic: z ? "公开" : "Public",
      badgePrivate: z ? "私密" : "Private",
      badgeNeedsSync: z ? "待同步" : "Needs sync",
      emptyRepositories: z
        ? "还没有 Push 到仓库的课程。在主页左侧对课程或课程包使用 Push。"
        : "No repository items yet. Use Push on a lesson or package from the home sidebar.",
      shareAfterPublic: z ? "设为公开后即可分享" : "Make it public before sharing",
      syncRepository: z ? "Sync" : "Sync",
      repositorySynced: z ? "Synced" : "Synced",
      pushRepositoryFail: z ? "Push 失败" : "Could not push to repository",
      transferToWorkspace: z ? "转移到本地" : "Transfer to workspace",
      transferToWorkspaceHint: z
        ? "用仓库版本恢复本地工作区，并打开以便继续编辑。"
        : "Restore the local workspace from the repository snapshot and open it for editing.",
      transferToWorkspaceFail: z ? "转移到本地失败" : "Could not transfer to workspace",
      visibilityUpdateFail: z ? "更新仓库可见性失败" : "Could not update repository visibility",
    },

    settings: {
      profile: {
        avatarAlt: z ? "用户头像" : "User avatar",
        publicLinkPrefix: z ? "公开链接：" : "Public URL: ",
        completeness: z ? "资料完整度" : "Profile completeness",
        visibilityLabel: z ? "公开范围" : "Visibility",
        visPrivate: z ? "仅自己" : "Private",
        visWorkspace: z ? "工作区" : "Workspace",
        visPublic: z ? "公开" : "Public",
        previewEyebrow: "Preview",
        bioPlaceholder: z ? "还没有填写个人简介。" : "No bio yet.",
        nameLabel: z ? "姓名" : "Name",
        usernameLabel: z ? "用户名" : "Username",
        usernameHint: z ? "3-32 位小写字母、数字或连字符。" : "3–32 chars: lowercase letters, digits or hyphen.",
        publicEmailLabel: z ? "公开电子邮件" : "Public email",
        publicEmailHidden: z ? "不公开" : "Hidden",
        bioLabel: z ? "个人简介" : "Bio",
        bioInputPlaceholder: z ? "请简单介绍一下你自己。" : "Tell people about yourself.",
        focusLabel: z ? "学习方向" : "Learning focus",
        urlLabel: "URL",
        locationLabel: z ? "地点" : "Location",
        locationPlaceholder: "Shanghai",
        companyLabel: z ? "机构" : "Organization",
        socialTitle: z ? "社交账号" : "Social links",
        socialPlaceholder: (n: number) => (z ? `链接到社交个人资料 ${n}` : `Social profile URL ${n}`),
        toggleEmail: z ? "公开邮箱" : "Show email",
        toggleEmailDesc: z ? "在公开资料中显示已选择的邮箱。" : "Show the selected email on your public profile.",
        toggleSocial: z ? "公开社交账号" : "Show social links",
        toggleSocialDesc: z ? "在个人主页展示社交链接。" : "Display social URLs on profile.",
        toggleRepos: z ? "展示个人项目" : "Show repositories",
        toggleReposDesc: z ? "在个人主页侧栏显示 repositories 数量。" : "Show repository count in the sidebar.",
        toggleStars: z ? "展示 Stars 收藏" : "Show starred courses",
        toggleStarsDesc: z ? "在个人主页侧栏显示收藏课程数量。" : "Show starred course count in the sidebar.",
        saveHelperOk: (url: string) => (z ? `公开资料会预览到 ${url}` : `Public profile previews at ${url}`),
        saveHelperInvalid: z ? "请先修正用户名。" : "Fix your username before saving.",
      },
      account: {
        loading: z ? "正在读取账户信息" : "Loading account…",
        fetchErrorFallback: z ? "无法读取账户信息" : "Could not load account",
        guestMessage: z ? "当前没有登录账户。" : "You are not signed in.",
        goLogin: z ? "去登录" : "Sign in",
        signOut: z ? "退出登录" : "Sign out",
        roleAdmin: z ? "管理员" : "Administrator",
        roleMember: z ? "普通用户" : "Member",
        createdLabel: z ? "创建于" : "Created",
        shortcutsTitle: z ? "快捷入口" : "Shortcuts",
        openStudio: z ? "课程工作台" : "Course studio",
        openAdmin: z ? "管理后台" : "Admin",
        metricRepos: z ? "个人项目" : "Repositories",
        metricStars: z ? "Stars 收藏" : "Stars",
        metricLastLogin: z ? "上次登录" : "Last sign-in",
        dateMissing: z ? "未记录" : "Unknown",
      },
      handleError: z
        ? "用户名需为 3-32 位小写字母、数字或连字符，并以字母或数字开头。"
        : "Username must be 3–32 chars (lowercase letters, digits or hyphen), starting with letter or digit.",
      notifications: {
        centerTitle: z ? "通知中心" : "Notification center",
        centerSummary: (count: number, start: string, end: string) =>
          z
            ? `已开启 ${count} 个通知来源，免打扰时段为 ${start}-${end}。`
            : `${count} notification source(s) enabled; quiet hours ${start}–${end}.`,
        browserPermTitle: z ? "浏览器权限" : "Browser permission",
        permUnsupported: z ? "不支持" : "Unsupported",
        permGranted: z ? "已允许" : "Allowed",
        permDenied: z ? "已拒绝" : "Denied",
        permDefault: z ? "未询问" : "Not asked",
        desktopTitle: z ? "桌面通知" : "Desktop notifications",
        desktopDesc: z ? "允许浏览器弹出课程和 AI 任务提醒。" : "Browser alerts for courses and AI jobs.",
        frequencyLabel: z ? "提醒频率" : "Frequency",
        freqInstant: z ? "即时" : "Instant",
        freqHourly: z ? "每小时" : "Hourly",
        freqDaily: z ? "每日摘要" : "Daily digest",
        allowBrowserBtn: z ? "允许浏览器通知" : "Allow notifications",
        sendTestBtn: z ? "发送测试通知" : "Send test",
        unsupportedBrowser: z ? "当前浏览器不支持桌面通知。" : "This browser does not support notifications.",
        enabledOk: z ? "桌面通知已启用。" : "Notifications enabled.",
        denied: z ? "浏览器没有授予桌面通知权限。" : "Notification permission denied.",
        requestFail: z ? "通知权限请求失败。" : "Could not request permission.",
        needAllowFirst: z ? "需要先允许浏览器通知。" : "Allow notifications first.",
        testSent: z ? "测试通知已发送。" : "Test notification sent.",
        testSendFail: z ? "测试通知发送失败。" : "Could not send test.",
        testTitle: z ? "开放课堂通知测试" : "OpenClass test notification",
        testBody: (start: string, end: string) =>
          z
            ? `课程活动、AI 结果和文稿变化会按 ${start}-${end} 的免打扰时段过滤。`
            : `Course, AI, and document alerts are filtered during quiet hours (${start}–${end}).`,
        courseActivityTitle: z ? "课程活动" : "Course activity",
        courseActivityDesc: z ? "课程包和讲义更新。" : "Packages and lessons.",
        weeklyTitle: z ? "每周摘要" : "Weekly digest",
        weeklyDesc: z ? "Stars 收藏和个人项目的周报。" : "Stars and repos summary.",
        aiTitle: z ? "AI 生成结果" : "AI results",
        aiDesc: z ? "长任务结束后提醒。" : "Alerts when long jobs finish.",
        resourceTitle: z ? "文稿变化" : "Document changes",
        resourceDesc: z ? "课程文稿生成、保存或恢复。" : "Document generation, saves, or restores.",
        quietStart: z ? "免打扰开始" : "Quiet hours start",
        quietEnd: z ? "免打扰结束" : "Quiet hours end",
        saveFooter: z ? "浏览器权限由当前浏览器控制，其他通知偏好保存到本机。" : "Browser permission is controlled by the browser; other prefs are saved locally.",
      },
      billing: {
        productTitle: z ? "开放课堂本地工作台" : "OpenClass local workspace",
        licenseSubtitle: z ? "Community License" : "Community License",
        enabledBadge: z ? "已启用" : "Active",
        viewAdmin: z ? "查看后台" : "Open admin",
        backHome: z ? "返回学习首页" : "Back to home",
        seatLabel: z ? "本地席位" : "Local seats",
      },
      email: {
        primaryLabel: z ? "主邮箱" : "Primary email",
        unbound: z ? "未绑定邮箱" : "No email linked",
        digestTitle: z ? "课程摘要邮件" : "Course digest email",
        digestDesc: z ? "每周发送学习项目和 Stars 收藏变化。" : "Weekly learning project and Stars changes.",
        aiMailTitle: z ? "AI 任务邮件" : "AI job email",
        aiMailDesc: z ? "长时间生成任务完成后发送。" : "Email when long generations finish.",
        securityTitle: z ? "安全邮件" : "Security email",
        securityDesc: z ? "登录、权限和账户安全变化。" : "Sign-ins, roles and security events.",
      },
      password: {
        currentLabel: z ? "当前密码" : "Current password",
        newLabel: z ? "新密码" : "New password",
        confirmLabel: z ? "确认新密码" : "Confirm new password",
        sessionTitle: z ? "会话保护" : "Session",
        sessionDesc: z ? "当前登录令牌仅保存在本机浏览器中。" : "Your sign-in token is stored only in this browser.",
        signOutEverywhere: z ? "退出所有本机会话" : "Sign out all browser sessions here",
        updateSubmit: z ? "更新密码" : "Update password",
        tooShort: z ? "新密码至少需要 8 位。" : "New password needs at least 8 characters.",
        mismatch: z ? "两次输入的新密码不一致。" : "New passwords do not match.",
        notAvailable: z ? "当前版本还没有开放密码修改接口。" : "Password change API is not available in this build yet.",
      },
      models: {
        loading: z ? "正在读取模型" : "Loading models…",
        fetchErrorFallback: z ? "无法读取模型配置" : "Could not load models",
        textDefaultTitle: z ? "文本默认" : "Default text model",
        realtimeDefaultTitle: z ? "实时语音默认" : "Default realtime model",
        textPrefLabel: z ? "偏好文本模型" : "Preferred text model",
        realtimePrefLabel: z ? "偏好实时语音模型" : "Preferred realtime model",
        autoOption: z ? "自动" : "Auto",
        modelNotConfigured: z ? "未配置" : "Not configured",
        modelConfigured: z ? "已配置" : "Configured",
        capabilityRealtime: z ? "实时语音" : "Realtime",
        capabilityText: z ? "文本生成" : "Text",
        codexTitle: z ? "ChatGPT / Codex 订阅" : "ChatGPT / Codex subscription",
        codexDisabled: z
          ? "后端未启用 Codex app-server。设置 OPENCLASS_CODEX_APP_SERVER_ENABLED=true 后可连接。"
          : "Codex app-server is disabled. Set OPENCLASS_CODEX_APP_SERVER_ENABLED=true to connect.",
        codexUnavailable: z ? "未找到 Codex CLI。请安装 Codex 或配置 OPENCLASS_CODEX_CLI_PATH。" : "Codex CLI was not found. Install Codex or set OPENCLASS_CODEX_CLI_PATH.",
        codexSignedIn: z ? "已连接" : "Connected",
        codexSignedOut: z ? "未连接" : "Not connected",
        codexLogin: z ? "连接 ChatGPT" : "Connect ChatGPT",
        codexLogout: z ? "断开" : "Disconnect",
        codexCancel: z ? "取消登录" : "Cancel login",
        codexOpen: z ? "打开验证页" : "Open verification page",
        codexCodeLabel: z ? "验证码" : "Code",
        codexWaiting: z ? "等待 OpenAI 授权完成" : "Waiting for OpenAI authorization",
      },
      security: {
        hidePathsTitle: z ? "隐藏本地路径" : "Hide local paths",
        hidePathsDesc: z ? "在个人页和导出信息中避免展示本机文件路径。" : "Avoid exposing local filesystem paths.",
        confirmLinksTitle: z ? "打开外部链接前确认" : "Confirm external links",
        confirmLinksDesc: z ? "跳转到外部学习资源前显示确认。" : "Prompt before visiting external URLs.",
        clearSessionTitle: z ? "关闭页面时清理会话" : "Clear session on exit",
        clearSessionDesc: z ? "离开浏览器后清除本机登录令牌。" : "Remove sign-in token when leaving.",
        discoveryTitle: z ? "允许课程被发现" : "Allow course discovery",
        discoveryDesc: z ? "公开资料页展示课程项目摘要。" : "Show summaries on public profile.",
      },
    },
  } as const;
}

export type HomeUiBundle = ReturnType<typeof profileSettingsTexts>["home"];
export type StudioUiBundle = ReturnType<typeof profileSettingsTexts>["studio"];

export function homeRelativeFormat(
  value: string | Date | null | undefined,
  texts: ReturnType<typeof profileSettingsTexts>["homeRelative"],
  locale: InterfaceLanguage
): string {
  if (!value) {
    return texts.justNow;
  }

  const date = value instanceof Date ? value : new Date(value);
  const timestamp = date.getTime();
  const intlLc = locale === "en" ? "en-US" : "zh-CN";

  if (Number.isNaN(timestamp)) {
    return texts.justNow;
  }

  const minutes = Math.floor((Date.now() - timestamp) / 60000);

  if (minutes <= 0) {
    return texts.justNow;
  }

  if (minutes < 60) {
    return texts.minutesAgo(minutes);
  }

  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return texts.hoursAgo(hours);
  }

  const days = Math.floor(hours / 24);
  if (days < 7) {
    return texts.daysAgo(days);
  }

  return new Intl.DateTimeFormat(intlLc, {
    month: "numeric",
    day: "numeric",
  }).format(date);
}
