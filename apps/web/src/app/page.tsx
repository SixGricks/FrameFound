import { redirect } from "next/navigation";

// The browse experience is the app until the full search UI lands in M6.
export default function Home() {
  redirect("/browse");
}
