// What the develop editor says while objects are being marked for removal.
//
// The final QA found two faults in one sentence: it told the reader to
// "press Remove" when the button is called "Remove marked", and it stayed on
// screen after Cancel marking, under a toolbar that had gone. The button's
// label and the sentence now come from here together, and the sentence goes
// when marking ends.

/** The button in the marking toolbar that sends the marks off. */
export const REMOVE_MARKED = "Remove marked";

/** Said while marking: what to do, naming the button that does it. */
export const MARKING_NOTICE = `Paint over what should disappear, then press ${REMOVE_MARKED}.`;

/**
 * The notice once marking has been turned on (`marking` true) or off. On:
 * the instruction. Off: the instruction goes, and anything else the page
 * has said since (a save, an added sky) stays.
 */
export function noticeAfterMarking(marking: boolean, notice: string | null): string | null {
  if (marking) return MARKING_NOTICE;
  return notice === MARKING_NOTICE ? null : notice;
}
