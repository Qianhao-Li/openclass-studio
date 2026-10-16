"use client";
import { useCallback, useState } from "react";

export function useRoutePanel<T>(requested: T) {
  const [state, setState] = useState({ route: requested, value: requested });
  if (state.route !== requested) setState({ route: requested, value: requested });
  const setValue = useCallback((value: T) => setState({ route: requested, value }), [requested]);
  return [state.value, setValue] as const;
}
