# Object Detection Model Setup

The vision stack supports an optional `ONNX` object detection model.

## Recommended File Name

Place the model file in this directory using a name such as:

```text
yolov8n.onnx
```

## Expected Workflow

1. Export or obtain an `ONNX` model compatible with OpenCV DNN.
2. Place the model file inside this `models/` directory.
3. In the GUI, set the model path for the camera panel.
4. Start the camera stream.

## Fallback Behavior

If no model file is available, the GUI still works in fallback mode.
Fallback mode uses a simple contour-based target detection overlay so the full vision workflow can still be demonstrated.

## Notes

- The repository does not include the model binary to keep the repository lightweight.
- If your exported model uses a custom class list, update the class names inside `main.py`.
- For best results, test the model on recorded underwater footage before deploying it live.

