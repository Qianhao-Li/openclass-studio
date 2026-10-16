import { NextResponse, type NextRequest } from "next/server";
export function proxy(request: NextRequest) {
 if (!["127.0.0.1","localhost","[::1]"].includes(request.nextUrl.hostname)) return new NextResponse("This Studio is local to its device.",{status:403});
 return NextResponse.next();
}
export const config={matcher:["/((?!api|_next/static|_next/image|favicon.ico|.*\\..*).*)"]};
