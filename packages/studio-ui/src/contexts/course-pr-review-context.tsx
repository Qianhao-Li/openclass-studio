"use client";

import { createContext, useContext, useCallback, useState, type ReactNode } from "react";
import type { CoursePr, CoursePrReview } from "../types/course-pr";

type ReviewContext = { pr: CoursePr; review: CoursePrReview; replaceReview: (review: CoursePrReview) => void; operationBusy: boolean; setOperationBusy: (busy: boolean) => void };
const Context = createContext<ReviewContext | null>(null);
export function CoursePrReviewProvider({ pr, initialReview, children }: { pr: CoursePr; initialReview: CoursePrReview; children: ReactNode }) {
  const [review, setReview] = useState(initialReview);
  const [operationBusy, setOperationBusy] = useState(false);
  const replaceReview = useCallback((next: CoursePrReview) => setReview((current) => next.id === current.id && next.draft_version >= current.draft_version ? next : current), []);
  return <Context.Provider value={{ pr, review, replaceReview, operationBusy, setOperationBusy }}>{children}</Context.Provider>;
}
export function useCoursePrReview() { return useContext(Context); }
