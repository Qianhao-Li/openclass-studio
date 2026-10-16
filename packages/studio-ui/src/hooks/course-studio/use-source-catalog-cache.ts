"use client";

import { useCallback, useMemo, useRef, useState } from "react";

import { useStudioApi } from "../../contexts/studio-api-context";
import type { SourceCatalogView, SourceIngestionRecord } from "../../types";

type CatalogCacheState = {
  byKey: Map<string, SourceCatalogView>;
  activeKeyBySourceId: Map<string, string>;
};

type SourceCatalogIdentity = {
  contentHash: string;
  updatedAt: string;
  catalogVersion: number;
};

const MAX_SOURCE_CATALOG_ENSURE_ATTEMPTS = 2;
const SOURCE_CATALOG_ENSURE_RETRY_DELAY_MS = 350;

const EMPTY_CACHE: CatalogCacheState = {
  byKey: new Map(),
  activeKeyBySourceId: new Map(),
};

export function sourceCatalogCacheKey(catalog: SourceCatalogView) {
  const versionIdentity = catalog.catalog_updated_at || `version-${catalog.catalog_version}`;
  return `${catalog.source.id}:${versionIdentity}`;
}

function sourceContentHash(source: SourceIngestionRecord) {
  const value = source.metadata?.content_hash;
  return typeof value === "string" ? value.trim() : "";
}

function compareCatalogUpdatedAt(left: string | null, right: string | null) {
  const leftValue = left?.trim() ?? "";
  const rightValue = right?.trim() ?? "";
  if (leftValue === rightValue) {
    return 0;
  }
  if (!leftValue) {
    return -1;
  }
  if (!rightValue) {
    return 1;
  }
  const leftTime = Date.parse(leftValue);
  const rightTime = Date.parse(rightValue);
  if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
    return leftTime > rightTime ? 1 : -1;
  }
  return leftValue > rightValue ? 1 : -1;
}

export function selectCurrentSourceCatalog(
  current: SourceCatalogView | null,
  incoming: SourceCatalogView,
  expectedContentHash = ""
) {
  if (!current) {
    return !expectedContentHash || incoming.source_content_hash === expectedContentHash
      ? incoming
      : null;
  }

  const currentVersion = Number.isFinite(current.catalog_version) ? current.catalog_version : 0;
  const incomingVersion = Number.isFinite(incoming.catalog_version) ? incoming.catalog_version : 0;
  if (incomingVersion < currentVersion) {
    return current;
  }
  if (incomingVersion > currentVersion) {
    if (expectedContentHash && incoming.source_content_hash !== expectedContentHash) {
      return current;
    }
    return incoming;
  }

  if (current.source_content_hash !== incoming.source_content_hash && expectedContentHash) {
    const currentMatches = current.source_content_hash === expectedContentHash;
    const incomingMatches = incoming.source_content_hash === expectedContentHash;
    if (currentMatches !== incomingMatches) {
      return incomingMatches ? incoming : current;
    }
    if (!currentMatches) {
      return current;
    }
  }

  const timestampOrder = compareCatalogUpdatedAt(
    incoming.catalog_updated_at,
    current.catalog_updated_at
  );
  if (timestampOrder < 0) {
    return current;
  }
  if (timestampOrder > 0) {
    return incoming;
  }
  if (current.source_content_hash !== incoming.source_content_hash) {
    return current;
  }
  return incoming;
}

function catalogSatisfiesSourceIdentity(
  catalog: SourceCatalogView | null,
  identity: SourceCatalogIdentity
) {
  if (!catalog) {
    return false;
  }
  if (catalog.chapter_count !== catalog.chapters.length) {
    return false;
  }
  // A late source-list response can still describe the previous file or catalog.
  // Its older hash/timestamp must not invalidate a catalog we already know is newer.
  if (identity.catalogVersion > 0 && catalog.catalog_version > identity.catalogVersion) {
    return true;
  }
  if (identity.contentHash && catalog.source_content_hash !== identity.contentHash) {
    return false;
  }
  if (catalog.catalog_version < identity.catalogVersion) {
    return false;
  }
  return (
    !identity.updatedAt ||
    compareCatalogUpdatedAt(catalog.catalog_updated_at, identity.updatedAt) >= 0
  );
}

function waitForCatalogRetry() {
  return new Promise<void>((resolve) => {
    window.setTimeout(resolve, SOURCE_CATALOG_ENSURE_RETRY_DELAY_MS);
  });
}

export type SourceCatalogCacheController = {
  catalogsBySourceId: ReadonlyMap<string, SourceCatalogView>;
  prefetchingPackageIds: ReadonlySet<string>;
  prefetchedPackageIds: ReadonlySet<string>;
  loadingSourceIds: ReadonlySet<string>;
  prefetchPackage: (packageId: string) => Promise<void>;
  ensureCurrentSource: (packageId: string, source: SourceIngestionRecord) => Promise<void>;
  refreshSource: (packageId: string, sourceId: string) => Promise<SourceCatalogView | null>;
  putCatalog: (catalog: SourceCatalogView) => SourceCatalogView | null;
  invalidateSource: (sourceId: string) => void;
  invalidateSources: (sourceIds: Iterable<string>) => void;
  clear: () => void;
};

export function useSourceCatalogCache(): SourceCatalogCacheController {
  const api = useStudioApi();
  const [cache, setCache] = useState<CatalogCacheState>(EMPTY_CACHE);
  const cacheRef = useRef(cache);
  const [prefetchingPackageIds, setPrefetchingPackageIds] = useState<Set<string>>(new Set());
  const [prefetchedPackageIds, setPrefetchedPackageIds] = useState<Set<string>>(new Set());
  const [loadingSourceIds, setLoadingSourceIds] = useState<Set<string>>(new Set());
  const prefetchedPackageIdsRef = useRef(new Set<string>());
  const packageRequestsRef = useRef(new Map<string, Promise<void>>());
  const sourceRequestsRef = useRef(new Map<string, Promise<SourceCatalogView | null>>());
  const ensuredSourceVersionsRef = useRef(new Set<string>());
  const currentSourceIdentityByIdRef = useRef(new Map<string, SourceCatalogIdentity>());

  const replaceCache = useCallback((next: CatalogCacheState) => {
    cacheRef.current = next;
    setCache(next);
  }, []);

  const putCatalogs = useCallback(
    (catalogs: SourceCatalogView[]) => {
      const effectiveCatalogs = new Map<string, SourceCatalogView>();
      if (!catalogs.length) {
        return effectiveCatalogs;
      }
      const current = cacheRef.current;
      const byKey = new Map(current.byKey);
      const activeKeyBySourceId = new Map(current.activeKeyBySourceId);
      for (const catalog of catalogs) {
        const sourceId = catalog.source.id;
        const previousKey = activeKeyBySourceId.get(sourceId);
        const previousCatalog = previousKey ? byKey.get(previousKey) ?? null : null;
        const expectedContentHash =
          currentSourceIdentityByIdRef.current.get(sourceId)?.contentHash ?? "";
        const effectiveCatalog = selectCurrentSourceCatalog(
          previousCatalog,
          catalog,
          expectedContentHash
        );
        if (!effectiveCatalog) {
          continue;
        }
        effectiveCatalogs.set(sourceId, effectiveCatalog);
        if (effectiveCatalog === previousCatalog) {
          continue;
        }
        const nextKey = sourceCatalogCacheKey(effectiveCatalog);
        if (previousKey && previousKey !== nextKey) {
          byKey.delete(previousKey);
        }
        byKey.set(nextKey, effectiveCatalog);
        activeKeyBySourceId.set(sourceId, nextKey);
      }
      replaceCache({ byKey, activeKeyBySourceId });
      return effectiveCatalogs;
    },
    [replaceCache]
  );

  const putCatalog = useCallback(
    (catalog: SourceCatalogView) => {
      return putCatalogs([catalog]).get(catalog.source.id) ?? null;
    },
    [putCatalogs]
  );

  const invalidateSources = useCallback(
    (sourceIds: Iterable<string>) => {
      const ids = new Set(sourceIds);
      if (!ids.size) {
        return;
      }
      const current = cacheRef.current;
      const byKey = new Map(current.byKey);
      const activeKeyBySourceId = new Map(current.activeKeyBySourceId);
      for (const sourceId of ids) {
        const activeKey = activeKeyBySourceId.get(sourceId);
        if (activeKey) {
          byKey.delete(activeKey);
        }
        activeKeyBySourceId.delete(sourceId);
        ensuredSourceVersionsRef.current.forEach((identity) => {
          if (identity.startsWith(`${sourceId}:`)) {
            ensuredSourceVersionsRef.current.delete(identity);
          }
        });
        currentSourceIdentityByIdRef.current.delete(sourceId);
      }
      replaceCache({ byKey, activeKeyBySourceId });
    },
    [replaceCache]
  );

  const invalidateSource = useCallback(
    (sourceId: string) => {
      invalidateSources([sourceId]);
    },
    [invalidateSources]
  );

  const prefetchPackage = useCallback(
    (packageId: string) => {
      if (!packageId || prefetchedPackageIdsRef.current.has(packageId)) {
        return Promise.resolve();
      }
      const existingRequest = packageRequestsRef.current.get(packageId);
      if (existingRequest) {
        return existingRequest;
      }
      setPrefetchingPackageIds((current) => new Set(current).add(packageId));
      const request = api
        .getPackageSourceCatalogs(packageId)
        .then((payload) => {
          putCatalogs(payload.catalogs);
          prefetchedPackageIdsRef.current.add(packageId);
          setPrefetchedPackageIds((current) => new Set(current).add(packageId));
        })
        .finally(() => {
          packageRequestsRef.current.delete(packageId);
          setPrefetchingPackageIds((current) => {
            const next = new Set(current);
            next.delete(packageId);
            return next;
          });
        });
      packageRequestsRef.current.set(packageId, request);
      return request;
    },
    [api, putCatalogs]
  );

  const refreshSource = useCallback(
    (packageId: string, sourceId: string) => {
      const requestIdentity = `${packageId}:${sourceId}`;
      const existingRequest = sourceRequestsRef.current.get(requestIdentity);
      if (existingRequest) {
        return existingRequest;
      }
      setLoadingSourceIds((current) => new Set(current).add(sourceId));
      const request = api
        .getPackageSourceCatalog(packageId, sourceId)
        .then((catalog) => {
          return putCatalog(catalog);
        })
        .finally(() => {
          sourceRequestsRef.current.delete(requestIdentity);
          setLoadingSourceIds((current) => {
            const next = new Set(current);
            next.delete(sourceId);
            return next;
          });
        });
      sourceRequestsRef.current.set(requestIdentity, request);
      return request;
    },
    [api, putCatalog]
  );

  const ensureCurrentSource = useCallback(
    async (packageId: string, source: SourceIngestionRecord) => {
      const hasPublishedSnapshot = Boolean(
        source.structure_updated_at &&
          (source.structure_status === "ready" || source.structure_status === "linear_only")
      );
      if (
        !prefetchedPackageIdsRef.current.has(packageId) ||
        (source.status !== "ready" && !hasPublishedSnapshot) ||
        source.structure_status === "pending" ||
        source.structure_status === "building"
      ) {
        return;
      }
      const activeKey = cacheRef.current.activeKeyBySourceId.get(source.id);
      const cachedCatalog = activeKey ? cacheRef.current.byKey.get(activeKey) ?? null : null;
      const advertisedUpdatedAt = source.structure_updated_at?.trim() || "";
      const advertisedContentHash = sourceContentHash(source);
      const sourceIdentity = {
        contentHash: advertisedContentHash,
        updatedAt: advertisedUpdatedAt,
        catalogVersion: typeof source.metadata.catalog_version === "number" && Number.isInteger(source.metadata.catalog_version)
          ? Math.max(0, source.metadata.catalog_version) : 0,
      };
      const previousIdentity = currentSourceIdentityByIdRef.current.get(source.id);
      if (
        !previousIdentity ||
        previousIdentity.contentHash !== sourceIdentity.contentHash ||
        previousIdentity.updatedAt !== sourceIdentity.updatedAt ||
        previousIdentity.catalogVersion !== sourceIdentity.catalogVersion
      ) {
        currentSourceIdentityByIdRef.current.set(source.id, sourceIdentity);
        ensuredSourceVersionsRef.current.forEach((identity) => {
          if (identity.startsWith(`${source.id}:`)) {
            ensuredSourceVersionsRef.current.delete(identity);
          }
        });
      }
      if (catalogSatisfiesSourceIdentity(cachedCatalog, sourceIdentity)) {
        return;
      }
      if (
        cachedCatalog &&
        advertisedContentHash &&
        cachedCatalog.source_content_hash !== advertisedContentHash
      ) {
        invalidateSource(source.id);
        currentSourceIdentityByIdRef.current.set(source.id, sourceIdentity);
      }
      const sourceVersionIdentity = `${advertisedUpdatedAt || source.updated_at}:${
        advertisedContentHash || "unknown-content"
      }:${sourceIdentity.catalogVersion}`;
      const ensureIdentity = `${source.id}:${sourceVersionIdentity}`;
      if (ensuredSourceVersionsRef.current.has(ensureIdentity)) {
        return;
      }
      ensuredSourceVersionsRef.current.add(ensureIdentity);
      try {
        for (let attempt = 0; attempt < MAX_SOURCE_CATALOG_ENSURE_ATTEMPTS; attempt += 1) {
          if (attempt > 0) {
            await waitForCatalogRetry();
          }
          const refreshedCatalog = await refreshSource(packageId, source.id);
          if (catalogSatisfiesSourceIdentity(refreshedCatalog, sourceIdentity)) {
            return;
          }
        }
        ensuredSourceVersionsRef.current.delete(ensureIdentity);
      } catch (error) {
        ensuredSourceVersionsRef.current.delete(ensureIdentity);
        throw error;
      }
    },
    [invalidateSource, refreshSource]
  );

  const clear = useCallback(() => {
    prefetchedPackageIdsRef.current.clear();
    packageRequestsRef.current.clear();
    sourceRequestsRef.current.clear();
    ensuredSourceVersionsRef.current.clear();
    currentSourceIdentityByIdRef.current.clear();
    setPrefetchingPackageIds(new Set());
    setPrefetchedPackageIds(new Set());
    setLoadingSourceIds(new Set());
    replaceCache({ byKey: new Map(), activeKeyBySourceId: new Map() });
  }, [replaceCache]);

  const catalogsBySourceId = useMemo(() => {
    const catalogs = new Map<string, SourceCatalogView>();
    cache.activeKeyBySourceId.forEach((key, sourceId) => {
      const catalog = cache.byKey.get(key);
      if (catalog) {
        catalogs.set(sourceId, catalog);
      }
    });
    return catalogs;
  }, [cache]);

  return {
    catalogsBySourceId,
    prefetchingPackageIds,
    prefetchedPackageIds,
    loadingSourceIds,
    prefetchPackage,
    ensureCurrentSource,
    refreshSource,
    putCatalog,
    invalidateSource,
    invalidateSources,
    clear,
  };
}
