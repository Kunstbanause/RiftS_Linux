// Headless OpenXR client: vibrates the controllers in a fixed pattern, to check the haptics.
// Per hand (left first): 3 strengths for 0.6 s each, then 10 short pulses (like game ticks),
// then 1.5 s of 2 ms pulses sent every frame (how OpenVR games buzz).
// Usage: hapticstest [left|right|both]   (needs a focused session: no game running)
// Build: gcc -O1 -Wall hapticstest.c -o hapticstest -lopenxr_loader
#include <time.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <openxr/openxr.h>

#define CHK(x) do { XrResult r_ = (x); if (XR_FAILED(r_)) { fprintf(stderr, "FAIL %s = %d\n", #x, r_); exit(1); } } while (0)

static XrInstance inst;
static XrSession sess;
static XrActionSet set;
static XrAction buzz;
static XrPath hands[2];
static int state = XR_SESSION_STATE_UNKNOWN;

static void sleep_ms(int ms)
{
	struct timespec ts = {ms / 1000, (ms % 1000) * 1000000L};
	nanosleep(&ts, NULL);
}

// Handle events and sync actions (Monado only sends haptics while the session is focused)
static void pump(void)
{
	XrEventDataBuffer ev = {XR_TYPE_EVENT_DATA_BUFFER};
	while (xrPollEvent(inst, &ev) == XR_SUCCESS) {
		if (ev.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
			state = ((XrEventDataSessionStateChanged *)&ev)->state;
			if (state == XR_SESSION_STATE_READY) {
				XrSessionBeginInfo bi = {XR_TYPE_SESSION_BEGIN_INFO};
				CHK(xrBeginSession(sess, &bi));
			}
		}
		ev = (XrEventDataBuffer){XR_TYPE_EVENT_DATA_BUFFER};
	}
	XrActiveActionSet aas = {set, XR_NULL_PATH};
	XrActionsSyncInfo si = {XR_TYPE_ACTIONS_SYNC_INFO, NULL, 1, &aas};
	xrSyncActions(sess, &si);
}

static void vibrate(int hand, float amplitude, int ms)
{
	XrHapticVibration v = {XR_TYPE_HAPTIC_VIBRATION};
	v.amplitude = amplitude;
	v.duration = (XrDuration)ms * 1000000;
	v.frequency = XR_FREQUENCY_UNSPECIFIED;
	XrHapticActionInfo hi = {XR_TYPE_HAPTIC_ACTION_INFO, NULL, buzz, hands[hand]};
	CHK(xrApplyHapticFeedback(sess, &hi, (XrHapticBaseHeader *)&v));
}

// Keep the session alive for ms while the driver plays the vibration
static void wait_ms(int ms)
{
	for (int t = 0; t < ms; t += 12) {
		pump();
		sleep_ms(12);
	}
}

static void pattern(int hand)
{
	const char *name = hand ? "right" : "left";
	const float strengths[] = {0.3f, 0.6f, 1.0f};
	for (int i = 0; i < 3; i++) {
		printf("%s: %.0f %% for 0.6 s\n", name, strengths[i] * 100);
		fflush(stdout);
		vibrate(hand, strengths[i], 600);
		wait_ms(1000);
	}
	printf("%s: 10 short pulses (full strength, 30 ms)\n", name);
	fflush(stdout);
	for (int i = 0; i < 10; i++) {
		vibrate(hand, 1.0f, 30);
		wait_ms(200);
	}
	wait_ms(400);
	printf("%s: 2 ms pulses every frame for 1.5 s (should feel like one continuous buzz)\n", name);
	fflush(stdout);
	for (int t = 0; t < 1500; t += 12) {
		vibrate(hand, 0.8f, 2);
		pump();
		sleep_ms(12);
	}
	wait_ms(1000);
}

int main(int argc, char **argv)
{
	const char *which = argc > 1 ? argv[1] : "both";
	const char *exts[] = {"XR_MND_headless"};
	XrInstanceCreateInfo ici = {XR_TYPE_INSTANCE_CREATE_INFO};
	strcpy(ici.applicationInfo.applicationName, "hapticstest");
	ici.applicationInfo.apiVersion = XR_MAKE_VERSION(1, 0, 0);
	ici.enabledExtensionCount = 1;
	ici.enabledExtensionNames = exts;
	CHK(xrCreateInstance(&ici, &inst));

	XrSystemGetInfo sgi = {XR_TYPE_SYSTEM_GET_INFO, NULL, XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY};
	XrSystemId sys;
	CHK(xrGetSystem(inst, &sgi, &sys));
	XrSessionCreateInfo sci = {XR_TYPE_SESSION_CREATE_INFO, NULL, 0, sys};
	CHK(xrCreateSession(inst, &sci, &sess));

	XrActionSetCreateInfo asci = {XR_TYPE_ACTION_SET_CREATE_INFO};
	strcpy(asci.actionSetName, "hapticstest");
	strcpy(asci.localizedActionSetName, "hapticstest");
	CHK(xrCreateActionSet(inst, &asci, &set));
	xrStringToPath(inst, "/user/hand/left", &hands[0]);
	xrStringToPath(inst, "/user/hand/right", &hands[1]);
	XrActionCreateInfo aci = {XR_TYPE_ACTION_CREATE_INFO};
	strcpy(aci.actionName, "buzz");
	strcpy(aci.localizedActionName, "buzz");
	aci.actionType = XR_ACTION_TYPE_VIBRATION_OUTPUT;
	aci.countSubactionPaths = 2;
	aci.subactionPaths = hands;
	CHK(xrCreateAction(set, &aci, &buzz));
	XrPath prof, lp, rp;
	xrStringToPath(inst, "/interaction_profiles/oculus/touch_controller", &prof);
	xrStringToPath(inst, "/user/hand/left/output/haptic", &lp);
	xrStringToPath(inst, "/user/hand/right/output/haptic", &rp);
	XrActionSuggestedBinding b[2] = {{buzz, lp}, {buzz, rp}};
	XrInteractionProfileSuggestedBinding sb = {XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING, NULL, prof, 2, b};
	CHK(xrSuggestInteractionProfileBindings(inst, &sb));
	XrSessionActionSetsAttachInfo att = {XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO, NULL, 1, &set};
	CHK(xrAttachSessionActionSets(sess, &att));

	for (int t = 0; t < 3000 && state != XR_SESSION_STATE_FOCUSED; t += 12) {
		pump();
		sleep_ms(12);
	}
	if (state != XR_SESSION_STATE_FOCUSED) {
		fprintf(stderr, "session never got focus (state %d): close the game / other OpenXR apps\n", state);
		return 1;
	}

	if (strcmp(which, "right") != 0)
		pattern(0);
	if (strcmp(which, "left") != 0)
		pattern(1);

	xrDestroyInstance(inst);
	return 0;
}
