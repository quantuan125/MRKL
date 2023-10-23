import chromadb
from chromadb.config import Settings
from langchain.embeddings import OpenAIEmbeddings
from langchain.vectorstores import Chroma
import streamlit as st
from utility.authy import Login

class ClientDB:
    def __init__(self, username, collection_name, load_vector_store=True):
        user_port = Login.get_port_for_user(username)
        if not user_port:
            raise ValueError(f"No server port found for user {username}")
        
        if username == "admin":
            auth_credentials = "admin:admin"
        else:
            auth_credentials = username 

        # Set up client with appropriate credentials and server URL
        self.client = chromadb.HttpClient(
            host="localhost",
            port=str(user_port),
            settings=Settings(
                chroma_client_auth_provider="chromadb.auth.basic.BasicAuthClientProvider",
                chroma_client_auth_credentials=auth_credentials,
                allow_reset=True
            )
        )
        #st.write(self.client)
        self.collection_name = collection_name
        self.vector_store = None
        if collection_name and load_vector_store:
            self.load_vector_store()

    def load_vector_store(self):
        embeddings = OpenAIEmbeddings()
        self.vector_store = Chroma(collection_name=self.collection_name, embedding_function=embeddings, client=self.client)
        st.session_state.vector_store = self.vector_store
        #st.write(st.session_state.vector_store)

    def get_existing_collections(self):
        collections = self.client.list_collections()
        sorted_collection = sorted([col.name for col in collections])
        return sorted_collection

    def reset_client(self):
        self.client.reset()

    