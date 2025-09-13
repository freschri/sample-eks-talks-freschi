import gradio as gr
from langchain_nvidia_ai_endpoints import ChatNVIDIA, NVIDIAEmbeddings
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import SentenceTransformersTokenTextSplitter
from langchain_milvus import Milvus
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain_core.prompts import ChatPromptTemplate
import os

LLM_URL=os.environ.get("LLM_URL", "http://localhost:8000/v1")
LLM_MODEL=os.environ.get("LLM_MODEL", "meta/llama-3.2-1b-instruct")
EMBEDDINGS_URL = os.environ.get("EMBEDDINGS_URL", "http://localhost:8001/v1")
EMBEDDINGS_MODEL = os.environ.get("EMBEDDINGS_MODEL", "nvidia/llama-3.2-nv-embedqa-1b-v2")
MILVUS_URL=os.environ.get("MILVUS_URL", "http://localhost:8002")
MILVUS_TOKEN=os.environ.get("MILVUS_TOKEN", "postgres:password")
MILVUS_DB_NAME=os.environ.get("MILVUS_DB_NAME", "milvus_demo")

embedder = NVIDIAEmbeddings(base_url=EMBEDDINGS_URL, model= EMBEDDINGS_MODEL)

text_splitter = SentenceTransformersTokenTextSplitter(
    chunk_overlap=100,
)

vectorstore = Milvus(
    embedder,
    connection_args={"uri": MILVUS_URL, "token": MILVUS_TOKEN},
    index_params={"index_type": "FLAT", "metric_type": "L2"},
    consistency_level="Strong",
    collection_name="rag_chatbot",
    drop_old=True,
    auto_id=True,
)

llm = ChatNVIDIA(
    base_url=LLM_URL,
    model=LLM_MODEL,
)

retriever = vectorstore.as_retriever()

prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Answer based on the following context:\n<Documents>\n{context}\n</Documents>",
        ),
        ("user", "{question}"),
    ]
)

chain = (
    {"context": retriever, "question": RunnablePassthrough()}
    | prompt
    | llm
    | StrOutputParser()
)

def predict(message, _):
    return chain.invoke(message)

def upload_file(files):
    for file_path in files:
        loader = PyPDFLoader(file_path)
        documents = loader.load()
        split_documents = text_splitter.split_documents(documents)
        vectorstore.add_documents(split_documents)
    return files

with gr.Blocks() as demo:
    with gr.Tab("Chat"):
        gr.ChatInterface(
            fn=predict, 
            type="messages",
        )
    with gr.Tab("Document upload"):
        with gr.Row():
            file_output = gr.File()
        with gr.Row():
            upload_button = gr.UploadButton("Click to upload documents", file_types=[".pdf"], file_count="multiple")
            upload_button.upload(
                fn=upload_file,
                inputs=upload_button,
                outputs=file_output
            )
    
demo.launch()
