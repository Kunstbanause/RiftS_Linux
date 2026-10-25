// Headless OpenXR client: logs both controllers' grip pose, velocity and tracked flag
// at ~80 Hz as CSV on stdout. Usage: posrec [seconds]
// Build: gcc -O1 -Wall posrec.c -o posrec -lopenxr_loader -lm
#define XR_USE_TIMESPEC
#include <time.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>

#define CHK(x) do { XrResult r_ = (x); if (XR_FAILED(r_)) { fprintf(stderr, "FAIL %s = %d\n", #x, r_); return 1; } } while (0)

int main(int argc, char **argv)
{
	int seconds = argc > 1 ? atoi(argv[1]) : 45;
	const char *exts[] = {"XR_MND_headless", "XR_KHR_convert_timespec_time"};
	XrInstanceCreateInfo ici = {XR_TYPE_INSTANCE_CREATE_INFO};
	strcpy(ici.applicationInfo.applicationName, "posrec");
	ici.applicationInfo.apiVersion = XR_MAKE_VERSION(1, 0, 0);
	ici.enabledExtensionCount = 2;
	ici.enabledExtensionNames = exts;
	XrInstance inst;
	CHK(xrCreateInstance(&ici, &inst));
	PFN_xrConvertTimespecTimeToTimeKHR conv;
	CHK(xrGetInstanceProcAddr(inst, "xrConvertTimespecTimeToTimeKHR", (PFN_xrVoidFunction *)&conv));

	XrSystemGetInfo sgi = {XR_TYPE_SYSTEM_GET_INFO, NULL, XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY};
	XrSystemId sys;
	CHK(xrGetSystem(inst, &sgi, &sys));
	XrSessionCreateInfo sci = {XR_TYPE_SESSION_CREATE_INFO, NULL, 0, sys};
	XrSession sess;
	CHK(xrCreateSession(inst, &sci, &sess));

	XrActionSetCreateInfo asci = {XR_TYPE_ACTION_SET_CREATE_INFO};
	strcpy(asci.actionSetName, "probe");
	strcpy(asci.localizedActionSetName, "probe");
	XrActionSet set;
	CHK(xrCreateActionSet(inst, &asci, &set));
	XrPath hands[2];
	xrStringToPath(inst, "/user/hand/left", &hands[0]);
	xrStringToPath(inst, "/user/hand/right", &hands[1]);
	XrActionCreateInfo aci = {XR_TYPE_ACTION_CREATE_INFO};
	strcpy(aci.actionName, "grip");
	strcpy(aci.localizedActionName, "grip");
	aci.actionType = XR_ACTION_TYPE_POSE_INPUT;
	aci.countSubactionPaths = 2;
	aci.subactionPaths = hands;
	XrAction grip;
	CHK(xrCreateAction(set, &aci, &grip));
	XrPath prof, lp, rp;
	xrStringToPath(inst, "/interaction_profiles/oculus/touch_controller", &prof);
	xrStringToPath(inst, "/user/hand/left/input/grip/pose", &lp);
	xrStringToPath(inst, "/user/hand/right/input/grip/pose", &rp);
	XrActionSuggestedBinding b[2] = {{grip, lp}, {grip, rp}};
	XrInteractionProfileSuggestedBinding sb = {XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING, NULL, prof, 2, b};
	CHK(xrSuggestInteractionProfileBindings(inst, &sb));
	XrSessionActionSetsAttachInfo att = {XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO, NULL, 1, &set};
	CHK(xrAttachSessionActionSets(sess, &att));

	XrReferenceSpaceCreateInfo rsci = {XR_TYPE_REFERENCE_SPACE_CREATE_INFO};
	rsci.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_LOCAL;
	rsci.poseInReferenceSpace.orientation.w = 1;
	XrSpace local, hs[2];
	CHK(xrCreateReferenceSpace(sess, &rsci, &local));
	for (int i = 0; i < 2; i++) {
		XrActionSpaceCreateInfo ascr = {XR_TYPE_ACTION_SPACE_CREATE_INFO, NULL, grip, hands[i]};
		ascr.poseInActionSpace.orientation.w = 1;
		CHK(xrCreateActionSpace(sess, &ascr, &hs[i]));
	}

	int running = 0;
	printf("t_s,side,tracked,px,py,pz,vx,vy,vz\n");
	for (int iter = 0; iter < seconds * 80; iter++) {
		XrEventDataBuffer ev = {XR_TYPE_EVENT_DATA_BUFFER};
		while (xrPollEvent(inst, &ev) == XR_SUCCESS) {
			if (ev.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED &&
			    ((XrEventDataSessionStateChanged *)&ev)->state == XR_SESSION_STATE_READY && !running) {
				XrSessionBeginInfo bi = {XR_TYPE_SESSION_BEGIN_INFO};
				CHK(xrBeginSession(sess, &bi));
				running = 1;
			}
			ev = (XrEventDataBuffer){XR_TYPE_EVENT_DATA_BUFFER};
		}
		if (running) {
			XrActiveActionSet aas = {set, XR_NULL_PATH};
			XrActionsSyncInfo si = {XR_TYPE_ACTIONS_SYNC_INFO, NULL, 1, &aas};
			xrSyncActions(sess, &si);
			struct timespec now;
			clock_gettime(CLOCK_MONOTONIC, &now);
			XrTime t;
			conv(inst, &now, &t);
			for (int i = 0; i < 2; i++) {
				XrSpaceVelocity vel = {XR_TYPE_SPACE_VELOCITY};
				XrSpaceLocation l = {XR_TYPE_SPACE_LOCATION, &vel};
				xrLocateSpace(hs[i], local, t, &l);
				printf("%.4f,%s,%d,%.5f,%.5f,%.5f,%.4f,%.4f,%.4f\n", iter / 80.0, i ? "R" : "L",
				       !!(l.locationFlags & XR_SPACE_LOCATION_POSITION_TRACKED_BIT), l.pose.position.x,
				       l.pose.position.y, l.pose.position.z, vel.linearVelocity.x, vel.linearVelocity.y,
				       vel.linearVelocity.z);
			}
			fflush(stdout);
		}
		struct timespec ts = {0, 12500000};
		nanosleep(&ts, NULL);
	}
	xrDestroyInstance(inst);
	return 0;
}
