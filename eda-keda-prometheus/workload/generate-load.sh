#!/bin/bash

# Load generator script for Redis queue scaling demo

echo "🚀 Starting Redis Queue Load Generator"
echo "======================================"

# Check if kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl not found. Please install kubectl first."
    exit 1
fi

# Function to generate sustained load (10 jobs for 2 minutes each)
generate_sustained_load() {
    echo "📊 Generating 10 long-running jobs (2 minutes each)..."
    echo "This will trigger scaling to 10 pods that stay busy for 2 minutes"
    
    for i in {1..10}; do
        kubectl exec -n workload deployment/redis -- redis-cli LPUSH demo_queue "{\"id\": $i, \"task\": \"long_process\", \"duration\": \"2min\"}"
        echo "✅ Added job $i"
    done
    
    echo ""
    echo "📈 Queue status:"
    QUEUE_LENGTH=$(kubectl exec -n workload deployment/redis -- redis-cli LLEN demo_queue)
    echo "Queue length: $QUEUE_LENGTH jobs"
    
    echo ""
    echo "🔍 Watch scaling with:"
    echo "kubectl get pods -n workload -w"
    echo ""
    echo "📊 Monitor queue length:"
    echo "kubectl exec -n workload deployment/redis -- redis-cli LLEN demo_queue"
}

# Function to generate burst load (50 quick jobs)
generate_burst_load() {
    echo "💥 Generating 50 quick jobs (5 seconds each)..."
    echo "This will trigger rapid scaling up and down"
    
    for i in {1..50}; do
        kubectl exec -n workload deployment/redis -- redis-cli LPUSH demo_queue "{\"id\": $i, \"task\": \"quick_process\", \"duration\": \"5sec\"}"
        echo "✅ Added job $i"
    done
    
    echo ""
    echo "📈 Queue status:"
    QUEUE_LENGTH=$(kubectl exec -n workload deployment/redis -- redis-cli LLEN demo_queue)
    echo "Queue length: $QUEUE_LENGTH jobs"
}

# Function to clear the queue
clear_queue() {
    echo "🧹 Clearing the queue..."
    kubectl exec -n workload deployment/redis -- redis-cli DEL demo_queue
    echo "✅ Queue cleared"
}

# Function to show queue status
show_status() {
    echo "📊 Current Status:"
    echo "=================="
    
    QUEUE_LENGTH=$(kubectl exec -n workload deployment/redis -- redis-cli LLEN demo_queue 2>/dev/null || echo "Redis not available")
    echo "Queue length: $QUEUE_LENGTH"
    
    echo ""
    echo "Worker pods:"
    kubectl get pods -n workload -l app.kubernetes.io/name=worker --no-headers 2>/dev/null || echo "No worker pods found"
    
    echo ""
    echo "ScaledObject status:"
    kubectl get scaledobject -n workload 2>/dev/null || echo "ScaledObject not found"
}

# Main menu
case "${1:-menu}" in
    "sustained")
        generate_sustained_load
        ;;
    "burst")
        generate_burst_load
        ;;
    "clear")
        clear_queue
        ;;
    "status")
        show_status
        ;;
    "menu"|*)
        echo "Redis Queue Load Generator"
        echo "========================="
        echo ""
        echo "Usage: $0 [command]"
        echo ""
        echo "Commands:"
        echo "  sustained  - Generate 10 jobs (2 min each) to keep 10 pods busy"
        echo "  burst      - Generate 50 quick jobs (5 sec each) for rapid scaling"
        echo "  clear      - Clear the queue"
        echo "  status     - Show current queue and pod status"
        echo ""
        echo "Examples:"
        echo "  $0 sustained   # Demo sustained scaling"
        echo "  $0 burst       # Demo burst scaling"
        echo "  $0 status      # Check current status"
        echo "  $0 clear       # Reset demo"
        ;;
esac
