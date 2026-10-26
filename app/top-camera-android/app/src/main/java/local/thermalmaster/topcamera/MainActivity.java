package local.thermalmaster.topcamera;

import android.Manifest;
import android.content.pm.PackageManager;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CaptureRequest;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Matrix;
import android.graphics.Rect;
import android.graphics.YuvImage;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Range;
import android.util.Log;
import android.view.Gravity;
import android.view.Surface;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.activity.ComponentActivity;
import androidx.camera.camera2.interop.Camera2CameraControl;
import androidx.camera.camera2.interop.Camera2CameraInfo;
import androidx.camera.camera2.interop.CaptureRequestOptions;
import androidx.camera.core.Camera;
import androidx.camera.core.CameraSelector;
import androidx.camera.core.ImageAnalysis;
import androidx.camera.core.ImageProxy;
import androidx.camera.core.Preview;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.google.common.util.concurrent.ListenableFuture;

import org.java_websocket.client.WebSocketClient;
import org.java_websocket.handshake.ServerHandshake;

import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.nio.ByteBuffer;
import org.json.JSONException;
import org.json.JSONObject;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

public final class MainActivity extends ComponentActivity {
    private static final String TAG = "TopCameraSender";
    private static final int CAMERA_PERMISSION_REQUEST = 42;
    private static final String SOCKET_URL = "ws://127.0.0.1:8000/ws/top-camera?token=thermal-master-top-camera-local";
    private static final int JPEG_QUALITY = 72;
    private static final long FRAME_INTERVAL_MS = 120L;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService cameraExecutor = Executors.newSingleThreadExecutor();
    private PreviewView previewView;
    private TextView statusView;
    private WebSocketClient socket;
    private volatile Camera camera;
    private volatile Camera2CameraControl camera2Control;
    private volatile boolean focusSupported = false;
    private volatile boolean exposureSupported = false;
    private volatile float minimumFocusDistance = 0f;
    private volatile int exposureMin = -6;
    private volatile int exposureMax = 6;
    private volatile int exposureIndex = 0;
    private volatile float focusPosition = 0.5f;
    private volatile String focusMode = "auto";
    private volatile long lastFrameAt = 0L;
    private volatile long sentFrameCount = 0L;
    private volatile boolean closing = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        buildUi();
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            startCamera();
        } else {
            ActivityCompat.requestPermissions(this, new String[]{Manifest.permission.CAMERA}, CAMERA_PERMISSION_REQUEST);
            setStatus("Camera permission is required");
        }
    }

    private void buildUi() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0xFF101417);

        previewView = new PreviewView(this);
        previewView.setScaleType(PreviewView.ScaleType.FILL_CENTER);
        root.addView(previewView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));

        statusView = new TextView(this);
        statusView.setTextColor(0xFFEAF4F1);
        statusView.setTextSize(14f);
        statusView.setGravity(Gravity.CENTER);
        statusView.setPadding(24, 12, 24, 12);
        statusView.setBackgroundColor(0xB820292C);
        FrameLayout.LayoutParams statusParams = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP | Gravity.CENTER_HORIZONTAL);
        statusParams.topMargin = 36;
        root.addView(statusView, statusParams);
        setContentView(root);
    }

    private void startCamera() {
        connectSocket();
        ListenableFuture<ProcessCameraProvider> providerFuture = ProcessCameraProvider.getInstance(this);
        providerFuture.addListener(() -> {
            try {
                ProcessCameraProvider provider = providerFuture.get();
                Preview preview = new Preview.Builder().build();
                preview.setSurfaceProvider(previewView.getSurfaceProvider());

                ImageAnalysis analysis = new ImageAnalysis.Builder()
                        .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                        .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
                        .build();
                analysis.setAnalyzer(cameraExecutor, image -> analyzeFrame(image));

                provider.unbindAll();
                camera = provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis);
                camera2Control = Camera2CameraControl.from(camera.getCameraControl());
                readCameraCapabilities(camera);
                sendCapabilities();
                setStatus(socket != null && socket.isOpen()
                        ? "Camera ready · PC connected"
                        : "Camera ready · connecting to PC");
            } catch (Exception exception) {
                setStatus("Camera failed: " + exception.getClass().getSimpleName());
            }
        }, ContextCompat.getMainExecutor(this));
    }

    private void connectSocket() {
        if (closing || (socket != null && socket.isOpen())) {
            return;
        }
        try {
            socket = new WebSocketClient(new URI(SOCKET_URL)) {
                @Override
                public void onOpen(ServerHandshake handshake) {
                    Log.i(TAG, "PC WebSocket connected");
                    sendCapabilities();
                    setStatus("Camera ready · PC connected");
                }

                @Override
                public void onMessage(String message) {
                    handleControlMessage(message);
                }

                @Override
                public void onClose(int code, String reason, boolean remote) {
                    Log.w(TAG, "PC WebSocket closed: " + code + " " + reason);
                    if (!closing) {
                        setStatus("Camera ready · PC disconnected");
                        scheduleReconnect();
                    }
                }

                @Override
                public void onError(Exception exception) {
                    Log.e(TAG, "PC WebSocket error", exception);
                    if (!closing) {
                        setStatus("Camera ready · waiting for PC");
                    }
                }
            };
            socket.setConnectionLostTimeout(10);
            Log.i(TAG, "Connecting to PC WebSocket");
            socket.connect();
        } catch (Exception exception) {
            Log.e(TAG, "Could not create PC WebSocket", exception);
            setStatus("Waiting for PC");
            scheduleReconnect();
        }
    }

    private void readCameraCapabilities(@NonNull Camera activeCamera) {
        try {
            Range<Integer> exposureRange = activeCamera.getCameraInfo()
                    .getExposureState()
                    .getExposureCompensationRange();
            exposureSupported = activeCamera.getCameraInfo().getExposureState().isExposureCompensationSupported();
            exposureMin = exposureRange.getLower();
            exposureMax = exposureRange.getUpper();

            Camera2CameraInfo camera2Info = Camera2CameraInfo.from(activeCamera.getCameraInfo());
            Float minimumDistance = camera2Info.getCameraCharacteristic(
                    CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE);
            minimumFocusDistance = minimumDistance == null ? 0f : Math.max(0f, minimumDistance);
            focusSupported = minimumFocusDistance > 0f;
        } catch (Exception exception) {
            Log.w(TAG, "Could not read manual camera capabilities", exception);
            focusSupported = false;
            exposureSupported = false;
            minimumFocusDistance = 0f;
        }
    }

    private void sendCapabilities() {
        if (socket == null || !socket.isOpen() || camera == null) {
            return;
        }
        try {
            JSONObject message = new JSONObject();
            message.put("type", "capabilities");
            message.put("ready", true);
            message.put("focusSupported", focusSupported);
            message.put("exposureSupported", exposureSupported);
            message.put("focusMode", focusMode);
            message.put("focusPosition", focusPosition);
            message.put("exposureIndex", exposureIndex);
            message.put("exposureMin", exposureMin);
            message.put("exposureMax", exposureMax);
            socket.send(message.toString());
        } catch (JSONException exception) {
            Log.e(TAG, "Could not encode camera capabilities", exception);
        }
    }

    private void handleControlMessage(String message) {
        if ("ready".equals(message)) {
            sendCapabilities();
            return;
        }
        try {
            JSONObject command = new JSONObject(message);
            if (!"camera_settings".equals(command.optString("type"))) {
                return;
            }
            mainHandler.post(() -> applyCameraSettings(command));
        } catch (JSONException exception) {
            Log.w(TAG, "Ignoring invalid camera control message", exception);
        }
    }

    private void applyCameraSettings(JSONObject command) {
        if (camera == null) {
            return;
        }

        if (command.has("exposureIndex")) {
            int requested = command.optInt("exposureIndex", exposureIndex);
            int clamped = Math.max(exposureMin, Math.min(exposureMax, requested));
            if (exposureSupported) {
                camera.getCameraControl().setExposureCompensationIndex(clamped);
                exposureIndex = clamped;
            }
        }

        String requestedMode = command.optString("focusMode", focusMode);
        if ("auto".equals(requestedMode)) {
            applyAutoFocus();
        } else if ("manual".equals(requestedMode) && command.has("focusPosition")) {
            applyManualFocus((float) command.optDouble("focusPosition", focusPosition));
        }
        sendControlApplied();
    }

    private void applyAutoFocus() {
        focusMode = "auto";
        if (camera2Control == null) {
            return;
        }
        CaptureRequestOptions options = new CaptureRequestOptions.Builder()
                .setCaptureRequestOption(
                        CaptureRequest.CONTROL_AF_MODE,
                        CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
                .build();
        camera2Control.setCaptureRequestOptions(options);
    }

    private void applyManualFocus(float requestedPosition) {
        if (!focusSupported || camera2Control == null) {
            applyAutoFocus();
            return;
        }
        focusPosition = Math.max(0f, Math.min(1f, requestedPosition));
        float focusDistance = minimumFocusDistance * focusPosition;
        CaptureRequestOptions options = new CaptureRequestOptions.Builder()
                .setCaptureRequestOption(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_OFF)
                .setCaptureRequestOption(CaptureRequest.LENS_FOCUS_DISTANCE, focusDistance)
                .build();
        camera2Control.setCaptureRequestOptions(options);
        focusMode = "manual";
    }

    private void sendControlApplied() {
        if (socket == null || !socket.isOpen()) {
            return;
        }
        try {
            JSONObject message = new JSONObject();
            message.put("type", "control_applied");
            message.put("focusSupported", focusSupported);
            message.put("exposureSupported", exposureSupported);
            message.put("focusMode", focusMode);
            message.put("focusPosition", focusPosition);
            message.put("exposureIndex", exposureIndex);
            socket.send(message.toString());
        } catch (JSONException exception) {
            Log.e(TAG, "Could not report camera control state", exception);
        }
    }

    private void scheduleReconnect() {
        if (!closing) {
            mainHandler.postDelayed(this::connectSocket, 2000L);
        }
    }

    private void analyzeFrame(@NonNull ImageProxy image) {
        try {
            long now = System.currentTimeMillis();
            if (now - lastFrameAt < FRAME_INTERVAL_MS || socket == null || !socket.isOpen()) {
                return;
            }
            byte[] jpeg = imageToJpeg(image);
            if (jpeg != null && socket.isOpen()) {
                socket.send(jpeg);
                lastFrameAt = now;
                sentFrameCount += 1L;
                if (sentFrameCount % 30L == 0L) {
                    Log.i(TAG, "Sent camera frames: " + sentFrameCount);
                }
            }
        } catch (Exception exception) {
            Log.e(TAG, "Could not encode or send camera frame", exception);
        } finally {
            image.close();
        }
    }

    private byte[] imageToJpeg(ImageProxy image) {
        ImageProxy.PlaneProxy[] planes = image.getPlanes();
        int width = image.getWidth();
        int height = image.getHeight();
        byte[] nv21 = new byte[width * height + (width * height / 2)];
        copyPlane(planes[0], width, height, nv21, 0, 1);
        int uvOffset = width * height;
        for (int row = 0; row < height / 2; row++) {
            for (int col = 0; col < width / 2; col++) {
                int uIndex = row * planes[1].getRowStride() + col * planes[1].getPixelStride();
                int vIndex = row * planes[2].getRowStride() + col * planes[2].getPixelStride();
                nv21[uvOffset++] = planes[2].getBuffer().get(vIndex);
                nv21[uvOffset++] = planes[1].getBuffer().get(uIndex);
            }
        }

        ByteArrayOutputStream output = new ByteArrayOutputStream();
        new YuvImage(nv21, android.graphics.ImageFormat.NV21, width, height, null)
                .compressToJpeg(new Rect(0, 0, width, height), JPEG_QUALITY, output);
        byte[] jpeg = output.toByteArray();
        int rotation = image.getImageInfo().getRotationDegrees();
        return rotation == 0 ? jpeg : rotateJpeg(jpeg, rotation);
    }

    private void copyPlane(ImageProxy.PlaneProxy plane, int width, int height, byte[] destination, int offset, int pixelStep) {
        ByteBuffer buffer = plane.getBuffer();
        int rowStride = plane.getRowStride();
        int pixelStride = plane.getPixelStride();
        int destinationIndex = offset;
        for (int row = 0; row < height; row++) {
            int rowStart = row * rowStride;
            for (int col = 0; col < width; col++) {
                destination[destinationIndex] = buffer.get(rowStart + col * pixelStride);
                destinationIndex += pixelStep;
            }
        }
    }

    private byte[] rotateJpeg(byte[] jpeg, int degrees) {
        Bitmap source = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length);
        if (source == null) {
            return jpeg;
        }
        Matrix matrix = new Matrix();
        matrix.postRotate(degrees);
        Bitmap rotated = Bitmap.createBitmap(source, 0, 0, source.getWidth(), source.getHeight(), matrix, true);
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        rotated.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, output);
        source.recycle();
        if (rotated != source) {
            rotated.recycle();
        }
        return output.toByteArray();
    }

    private void setStatus(String status) {
        mainHandler.post(() -> {
            if (statusView != null) {
                statusView.setText(status);
            }
        });
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions, @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == CAMERA_PERMISSION_REQUEST && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startCamera();
        } else {
            setStatus("Camera permission denied");
        }
    }

    @Override
    protected void onDestroy() {
        closing = true;
        if (socket != null) {
            socket.close();
        }
        cameraExecutor.shutdownNow();
        try {
            cameraExecutor.awaitTermination(1, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        super.onDestroy();
    }
}
