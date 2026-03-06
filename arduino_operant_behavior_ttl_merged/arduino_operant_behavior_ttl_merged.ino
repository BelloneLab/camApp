/*
 * Merged operant behavior + TTL interface for camApp.
 */

#ifndef Serial0
#define Serial0 Serial
#endif

#define LED_ON LOW
#define LED_OFF HIGH
#define LED_GREEN 45

#define LED_BLUE 21
#define LED_RED 46
#define Lever 14 //Lever press input
//#define Lickometer 13 //This is the Lickometer value

//Deal with Servo Motor
#include <ESP32Servo.h>
#define MotorLeverPressA 13
Servo myservo;  // create servo object to control a servo

int posServo;
unsigned long DurationCue = 2000; //Duration of the Cue presentation before lever presentation

// Define Duration for operant task
int DurationLeverPresentation = 10000; //Duration during which the animal can press the lever

//Defin InterTrial Interval
unsigned long DurationITI = 2000;

int LeverPress;
unsigned long TimeLeverPresentedBegin;
unsigned long TimeLeverPresentedCurrent;
unsigned long LeverPressTime;
int Lick;
int TimeLick;
int NewCountLick;
int CountLick;

// ===== Added merged TTL runtime =====

static const int GATE_PIN = 3;
static const int SYNC_PIN = 9;
static const int BARCODE_PIN = 18;

bool ttlEngineEnabled = false;
bool ttlPinsInitialized = false;

bool stateGate = false;
bool stateSync = false;
bool stateBarcode0 = false;
bool stateBarcode1 = false;
bool stateLever = false;
bool stateCue = false;
bool stateReward = false;
bool stateIti = false;

uint32_t countGate = 0;
uint32_t countSync = 0;
uint32_t countBarcode = 0;
uint32_t countLever = 0;
uint32_t countCue = 0;
uint32_t countReward = 0;
uint32_t countIti = 0;

uint32_t edgeGateMs = 0;
uint32_t edgeSyncMs = 0;
uint32_t edgeBarcodeMs = 0;
uint32_t edgeLeverMs = 0;
uint32_t edgeCueMs = 0;
uint32_t edgeRewardMs = 0;
uint32_t edgeItiMs = 0;

bool prevGate = false;
bool prevSync = false;
bool prevBarcode0 = false;
bool prevLever = false;
bool prevCue = false;
bool prevReward = false;
bool prevIti = false;

bool syncOutputState = false;
uint8_t barcodeStep = 0;
unsigned long lastSyncToggleMs = 0;
unsigned long lastBarcodeStepMs = 0;

String serialCommandBuffer;
bool loopAnnounced = false;
bool serialStateInitialized = false;
bool serialGateState = false;
bool serialSyncState = false;
bool serialBarcodeState = false;
bool serialLeverState = false;
bool serialCueState = false;
bool serialRewardState = false;
bool serialItiState = false;

void initTtlPinsIfNeeded() {
  if (ttlPinsInitialized) {
    return;
  }
  pinMode(GATE_PIN, OUTPUT);
  pinMode(SYNC_PIN, OUTPUT);
  pinMode(BARCODE_PIN, OUTPUT);
  digitalWrite(GATE_PIN, LOW);
  digitalWrite(SYNC_PIN, LOW);
  digitalWrite(BARCODE_PIN, LOW);
  ttlPinsInitialized = true;
}

void clearTtlOutputs() {
  if (!ttlPinsInitialized) {
    return;
  }
  digitalWrite(GATE_PIN, LOW);
  digitalWrite(SYNC_PIN, LOW);
  digitalWrite(BARCODE_PIN, LOW);
}

void setTtlEngineEnabled(bool enabled) {
  ttlEngineEnabled = enabled;
  if (enabled) {
    initTtlPinsIfNeeded();
    syncOutputState = false;
    barcodeStep = 0;
    lastSyncToggleMs = millis();
    lastBarcodeStepMs = millis();
  } else {
    clearTtlOutputs();
  }
}

void emitPinConfig() {
  Serial0.println("GATE:3,SYNC:9,BARCODE:18,LEVER:14,CUE:45,REWARD:21,ITI:46");
}

void emitStatePacket() {
  Serial0.print("gate:");
  Serial0.print(stateGate ? 1 : 0);
  Serial0.print(",sync:");
  Serial0.print(stateSync ? 1 : 0);
  Serial0.print(",barcode0:");
  Serial0.print(stateBarcode0 ? 1 : 0);
  Serial0.print(",barcode1:");
  Serial0.print(stateBarcode1 ? 1 : 0);
  Serial0.print(",lever:");
  Serial0.print(stateLever ? 1 : 0);
  Serial0.print(",cue:");
  Serial0.print(stateCue ? 1 : 0);
  Serial0.print(",reward:");
  Serial0.print(stateReward ? 1 : 0);
  Serial0.print(",iti:");
  Serial0.print(stateIti ? 1 : 0);

  Serial0.print(",gate_edge_ms:");
  Serial0.print(edgeGateMs);
  Serial0.print(",sync_edge_ms:");
  Serial0.print(edgeSyncMs);
  Serial0.print(",barcode_edge_ms:");
  Serial0.print(edgeBarcodeMs);
  Serial0.print(",lever_edge_ms:");
  Serial0.print(edgeLeverMs);
  Serial0.print(",cue_edge_ms:");
  Serial0.print(edgeCueMs);
  Serial0.print(",reward_edge_ms:");
  Serial0.print(edgeRewardMs);
  Serial0.print(",iti_edge_ms:");
  Serial0.print(edgeItiMs);

  Serial0.print(",gate_count:");
  Serial0.print(countGate);
  Serial0.print(",sync_count:");
  Serial0.print(countSync);
  Serial0.print(",barcode_count:");
  Serial0.print(countBarcode);
  Serial0.print(",lever_count:");
  Serial0.print(countLever);
  Serial0.print(",cue_count:");
  Serial0.print(countCue);
  Serial0.print(",reward_count:");
  Serial0.print(countReward);
  Serial0.print(",iti_count:");
  Serial0.println(countIti);
}

void updateRisingEdge(bool currentState, bool &previousState, uint32_t &counter, uint32_t &edgeTimestamp, unsigned long nowMs) {
  if (currentState && !previousState) {
    counter++;
    edgeTimestamp = nowMs;
  }
  previousState = currentState;
}

void emitLevelEvent(const char *label, bool isHigh) {
  Serial0.print(label);
  Serial0.println(isHigh ? "_ON" : "_OFF");
}

void emitTransitions() {
  if (!serialStateInitialized) {
    serialGateState = stateGate;
    serialSyncState = stateSync;
    serialBarcodeState = stateBarcode0;
    serialLeverState = stateLever;
    serialCueState = stateCue;
    serialRewardState = stateReward;
    serialItiState = stateIti;
    serialStateInitialized = true;
    return;
  }

  if (stateGate != serialGateState) {
    serialGateState = stateGate;
    emitLevelEvent("GATE", stateGate);
  }
  if (stateSync != serialSyncState) {
    serialSyncState = stateSync;
    emitLevelEvent("SYNC", stateSync);
  }
  if (stateBarcode0 != serialBarcodeState) {
    serialBarcodeState = stateBarcode0;
    emitLevelEvent("BARCODE", stateBarcode0);
  }
  if (stateLever != serialLeverState) {
    serialLeverState = stateLever;
    emitLevelEvent("LEVER", stateLever);
  }
  if (stateCue != serialCueState) {
    serialCueState = stateCue;
    emitLevelEvent("CUE", stateCue);
  }
  if (stateReward != serialRewardState) {
    serialRewardState = stateReward;
    emitLevelEvent("REWARD", stateReward);
  }
  if (stateIti != serialItiState) {
    serialItiState = stateIti;
    emitLevelEvent("ITI", stateIti);
  }
}

void handleSerialCommand(const String &rawCommand) {
  String command = rawCommand;
  command.trim();
  command.toUpperCase();

  if (command.length() == 0) {
    return;
  }

  if (command == "GET_PINS") {
    emitPinConfig();
    return;
  }

  if (command == "GET_STATES") {
    emitStatePacket();
    return;
  }

  if (command == "START_TEST") {
    setTtlEngineEnabled(true);
    Serial0.println("OK_TEST");
    return;
  }

  if (command == "START_RECORDING") {
    setTtlEngineEnabled(true);
    Serial0.println("OK_RECORDING");
    return;
  }

  if (command == "STOP_TEST" || command == "STOP_RECORDING") {
    setTtlEngineEnabled(false);
    Serial0.println("OK_STOPPED");
    return;
  }
}

void serviceSerialCommands() {
  int budget = 64;
  while (Serial0.available() > 0 && budget > 0) {
    char c = (char)Serial0.read();
    if (c == '\n' || c == '\r') {
      if (serialCommandBuffer.length() > 0) {
        handleSerialCommand(serialCommandBuffer);
        serialCommandBuffer = "";
      }
    } else {
      if (serialCommandBuffer.length() < 120) {
        serialCommandBuffer += c;
      } else {
        // Guard against runaway/no-newline input.
        serialCommandBuffer = "";
      }
    }
    budget--;
  }
}

void serviceMergedRuntime() {
  serviceSerialCommands();

  unsigned long nowMs = millis();

  bool gateNow = false;
  bool syncNow = false;
  bool barcode0Now = false;

  if (ttlEngineEnabled) {
    if (nowMs - lastSyncToggleMs >= 500) {
      syncOutputState = !syncOutputState;
      lastSyncToggleMs = nowMs;
    }

    if (nowMs - lastBarcodeStepMs >= 100) {
      barcodeStep ^= 0x01;
      lastBarcodeStepMs = nowMs;
    }

    gateNow = true;
    syncNow = syncOutputState;
    barcode0Now = (barcodeStep != 0);

    initTtlPinsIfNeeded();
    digitalWrite(GATE_PIN, gateNow ? HIGH : LOW);
    digitalWrite(SYNC_PIN, syncNow ? HIGH : LOW);
    digitalWrite(BARCODE_PIN, barcode0Now ? HIGH : LOW);
  } else {
    clearTtlOutputs();
  }

  bool leverNow = (digitalRead(Lever) == LOW);
  bool cueNow = (digitalRead(LED_GREEN) == LED_ON);
  bool rewardNow = (digitalRead(LED_BLUE) == LED_ON);
  bool itiNow = cueNow && rewardNow;

  stateGate = gateNow;
  stateSync = syncNow;
  stateBarcode0 = barcode0Now;
  stateBarcode1 = barcode0Now;
  stateLever = leverNow;
  stateCue = cueNow;
  stateReward = rewardNow;
  stateIti = itiNow;

  updateRisingEdge(gateNow, prevGate, countGate, edgeGateMs, nowMs);
  updateRisingEdge(syncNow, prevSync, countSync, edgeSyncMs, nowMs);
  updateRisingEdge(barcode0Now, prevBarcode0, countBarcode, edgeBarcodeMs, nowMs);
  updateRisingEdge(leverNow, prevLever, countLever, edgeLeverMs, nowMs);
  updateRisingEdge(cueNow, prevCue, countCue, edgeCueMs, nowMs);
  updateRisingEdge(rewardNow, prevReward, countReward, edgeRewardMs, nowMs);
  updateRisingEdge(itiNow, prevIti, countIti, edgeItiMs, nowMs);
  emitTransitions();
}

void delayWithService(unsigned long durationMs) {
  unsigned long startMs = millis();
  while (millis() - startMs < durationMs) {
    serviceMergedRuntime();
    delay(1);
  }
}

void setup() {
  Serial0.begin(9600);
  delay(30);
  Serial0.println("FW:OPERANT_TTL_MERGED_V3_9600");
  Serial0.println("BOOT_OK");
  
  pinMode(LED_GREEN, OUTPUT);
  digitalWrite(LED_GREEN, LED_OFF);
  
  pinMode(LED_BLUE, OUTPUT);
  pinMode(Lever, INPUT);
  pinMode(MotorLeverPressA, INPUT);

  
  digitalWrite(LED_BLUE, LED_OFF);
  digitalWrite(LED_RED, LED_OFF);

  myservo.attach(MotorLeverPressA);
  Serial0.println("SERVO_OK");

  for (int i = 0; i < 5; i++) {
    Serial0.println("READY");
    delay(80);
  }
}


void loop() {

  if (!loopAnnounced) {
    Serial0.println("LOOP_STARTED");
    loopAnnounced = true;
  }

  serviceMergedRuntime();

  //Initial Cue
  Serial0.println("CUE_ON");
  digitalWrite(LED_GREEN, LED_ON);
  delayWithService(DurationCue);
  Serial0.println("CUE_OFF");
  serviceMergedRuntime();
  digitalWrite(LED_GREEN, LED_OFF);

  //Present Lever
    for (posServo = 160; posServo >= 20; posServo -= 1) { // goes from 160 degrees to 0 degrees
    myservo.write(posServo);
    delayWithService(10);
  }


  //Delay during which the mice can press the lever
  TimeLeverPresentedBegin = millis();
  TimeLeverPresentedCurrent = millis();

  while(TimeLeverPresentedCurrent - TimeLeverPresentedBegin <= DurationLeverPresentation){
    TimeLeverPresentedCurrent = millis();
    serviceMergedRuntime();

    LeverPress = digitalRead(Lever);
   
    if(LeverPress == LOW){ //Lever was pressed

      //Deliver Reward
      //Reward Delivery goes here
      Serial0.println("REWARD_ON");
      digitalWrite(LED_BLUE, LED_ON);
      delayWithService(1000);
      serviceMergedRuntime();
      Serial0.println("REWARD_OFF");
      digitalWrite(LED_BLUE, LED_OFF);
      LeverPressTime = millis();
      Serial0.println(LeverPressTime);
      Serial0.println("Lever was pressed"); //Write time of lever press

      //End Loop
      TimeLeverPresentedCurrent = DurationLeverPresentation + TimeLeverPresentedBegin + 1000;
      Serial0.println(TimeLeverPresentedCurrent);

    }
    

  }
  //Retract Lever
  for (posServo = 20; posServo <= 160; posServo += 1) { // goes from 0 degrees to 160 degrees
    // in steps of 1 degree
    myservo.write(posServo);              // tell servo to go to position in variable 'posServo'   
    delayWithService(10);  // waits 15ms for the servo to reach the position
  }

  digitalWrite(LED_BLUE, LED_ON);
  digitalWrite(LED_GREEN, LED_ON);
  Serial0.println("ITI_ON");
  delayWithService(DurationITI); //Inter Trial Interval
  serviceMergedRuntime();
  digitalWrite(LED_BLUE, LED_OFF);
  digitalWrite(LED_GREEN, LED_OFF);
  Serial0.println("ITI_OFF");

}


